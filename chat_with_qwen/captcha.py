# -*- coding: utf-8 -*-
"""验证码 / 滑块 / Midscene 相关代码。"""
import asyncio
import json
import re
import time
import base64
import math
import random
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

from . import config
from .state import log


async def detect_captcha(page):
    visible_hits = []
    for sel in config.CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=100):
                visible_hits.append(sel)
                break
        except Exception:
            continue
    try:
        kw_hit = await page.evaluate("""() => {
            const kws = [%s];
            const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
            let count = 0;
            let node;
            const sample = [];
            while ((node = walker.nextNode()) && count < 2000) {
                const t = node.textContent.trim();
                if (t && t.length < 50) {
                    for (const k of kws) {
                        if (t.includes(k)) {
                            if (sample.length < 3) sample.push(t);
                            count++;
                            break;
                        }
                    }
                }
            }
            return { count, sample };
        }()""" % (",".join([f'"{k}"' for k in config.CAPTCHA_KEYWORDS])))
        kw_count = kw_hit.get("count", 0) if isinstance(kw_hit, dict) else 0
    except Exception:
        kw_count = 0
    found = bool(visible_hits or kw_count >= 2)
    if found:
        now = time.time()
        if now - config.LAST_CAPTCHA_ALERT > config.CAPTCHA_ALERT_COOLDOWN:
            config.LAST_CAPTCHA_ALERT = now
            detail = []
            if visible_hits:
                detail.append(f"元素: {visible_hits[:3]}")
            log("\n" + "=" * 60)
            log("⚠️  检测到滑块/人机验证！")
            if detail:
                log(f"   详情: {', '.join(detail)}")

            # ---- 先轮询等待滑块弹窗真正渲染完成 ----
            # 放在诊断截图之前, 保证诊断截图与 DOM 候选检测都能拍到已渲染的滑块。
            await _wait_captcha_rendered(page)

            # ---- 滑块诊断: 截图 + DOM 信息 ----
            if config.CAPTCHA_DIAGNOSE:
                await _captcha_diagnose(page, visible_hits)

            # ---- 自动尝试滑动 ----
            auto_success = False
            if AUTO_SLIDE_CAPTCHA:
                auto_success = await _try_auto_slide_captcha(page)

            if not auto_success:
                log("   请在浏览器中手动完成验证")
                log("   （划动滑块或完成验证后，脚本会自动继续）")
                log("=" * 60 + "\n")
            else:
                log("=" * 60 + "\n")

            # 如果自动滑动成功, 返回 False 表示 captcha 已解决
            if auto_success:
                return False

    return found


async def _wait_captcha_rendered(page, max_wait=None, poll=None):
    """轮询等待滑块弹窗真正渲染完成 (替代固定 sleep)。

    很多情况下 captcha iframe 已挂到 DOM, 但内部手柄/提示还没画出来 (弹窗一片空白),
    此时截图/定位都会失败。这里轮询检测验证码 frame 内是否已出现可拖拽手柄或提示文案,
    渲染好则立即返回 True (通常远快于固定 3 秒); 超过 max_wait 仍未就绪则返回 False,
    交由后续流程继续尝试 (不阻断)。
    """
    if max_wait is None:
        max_wait = config.CAPTCHA_RENDER_MAX_WAIT
    if poll is None:
        poll = config.CAPTCHA_RENDER_POLL

    # 验证码 frame 的 URL 特征 (含阿里 tmd/punish 滑块)
    frame_url_markers = ("captcha", "verify", "security", "validate", "punish", "x5sec")
    # frame 内"已渲染"信号: 手柄/轨道类选择器, 或提示文案关键词
    check_js = """() => {
        try {
            const sels = [
                '[class*="slider"]', '[class*="Slider"]', '[class*="btn_slide"]',
                '[class*="nc-"]', '[class*="nc_"]', '[class*="handle"]',
                '[class*="track"]', '[class*="scale"]', '[class*="drag"]',
                '.nc_iconfont', '.btn_slide', '.nc_scale'
            ];
            for (const s of sels) {
                for (const el of document.querySelectorAll(s)) {
                    if (!el.getBoundingClientRect) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 8) return true;
                }
            }
            const txt = (document.body ? (document.body.innerText || '') : '');
            const kws = ['请按住滑块', '拖动', '拖到', '滑动', '完成验证', '滑块', '拼图', '缺口'];
            for (const k of kws) { if (txt.includes(k)) return true; }
        } catch (e) {}
        return false;
    }"""

    deadline = time.time() + max_wait
    log(f"   ⏳ 等待滑块弹窗渲染 (轮询, 最长 {max_wait:.0f}s)...")
    while time.time() < deadline:
        # 收集候选 frame: 匹配 URL 特征的 frame + 主 frame (滑块可能直接在主页面)
        target_frames = []
        for fr in page.frames:
            try:
                u = (fr.url or "").lower()
            except Exception:
                u = ""
            if any(m in u for m in frame_url_markers):
                target_frames.append(fr)
        try:
            target_frames.append(page.main_frame)
        except Exception:
            pass

        for fr in target_frames:
            try:
                rendered = await asyncio.wait_for(fr.evaluate(check_js), timeout=1.5)
            except Exception:
                rendered = False
            if rendered:
                elapsed = max_wait - (deadline - time.time())
                log(f"   ✅ 滑块已渲染 (耗时 {elapsed:.1f}s)")
                await asyncio.sleep(config.CAPTCHA_RENDER_SETTLE)
                return True

        await asyncio.sleep(poll)

    log(f"   ⚠️ 等待 {max_wait:.0f}s 仍未确认滑块渲染, 继续尝试...")
    return False


async def _captcha_diagnose(page, visible_selectors):
    """检测到滑块后, 等待渲染完成, 截图并保存 DOM 结构信息, 用于分析滑块位置和样式"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    config.RAW_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    viewport_png = config.RAW_DUMP_DIR / f"captcha_{ts}_viewport.png"
    fullpage_png = config.RAW_DUMP_DIR / f"captcha_{ts}_fullpage.png"
    info_json = config.RAW_DUMP_DIR / f"captcha_{ts}_info.json"

    try:
        # 等待滑块完全渲染(避免半渲染状态)
        await asyncio.sleep(0.8)

        # 1. 视口截图(与 page.mouse 坐标系一致, 用于后续视觉定位)
        await page.screenshot(path=str(viewport_png), full_page=False)
        log(f"📸 [诊断] 视口截图已保存: {viewport_png}")

        # 2. 全页截图(参考用)
        await page.screenshot(path=str(fullpage_png), full_page=True)
        log(f"📸 [诊断] 全页截图已保存: {fullpage_png}")

        # 3. 获取视口 CSS 像素尺寸(连接已有 Chrome 时 viewport_size 可能为 None, 用 JS 兜底)
        vp_info = {"width": 0, "height": 0}
        try:
            vp = page.viewport_size
            if vp:
                vp_info = {"width": vp["width"], "height": vp["height"]}
            else:
                # 通过 JS 获取
                js_vp = await page.evaluate("""() => ({
                    width: window.innerWidth || document.documentElement.clientWidth || 0,
                    height: window.innerHeight || document.documentElement.clientHeight || 0,
                    dpr: window.devicePixelRatio || 1,
                })""")
                vp_info = {
                    "width": js_vp.get("width", 0),
                    "height": js_vp.get("height", 0),
                    "dpr": js_vp.get("dpr", 1),
                }
        except Exception:
            pass

        # 4. 收集 DOM 信息: 遍历所有 frame
        frames_info = []
        for i, frame in enumerate(page.frames):
            try:
                frame_info = {
                    "index": i,
                    "url": frame.url[:200] if frame.url else "(main)",
                }
                try:
                    # 在每个 frame 内查找候选滑块元素
                    # 注意: Playwright 的 Frame.evaluate() 不接受 timeout 关键字参数,
                    # 用 asyncio.wait_for 包裹以保留"防卡死"超时保护。
                    candidates = await asyncio.wait_for(frame.evaluate("""(selectors) => {
                        const results = [];
                        for (const sel of selectors) {
                            try {
                                const els = document.querySelectorAll(sel);
                                for (const el of els) {
                                    if (!el.getBoundingClientRect) continue;
                                    const r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {
                                        results.push({
                                            selector: sel,
                                            bbox: {x: Math.round(r.x), y: Math.round(r.y),
                                                   w: Math.round(r.width), h: Math.round(r.height)},
                                            text: (el.textContent || '').trim().substring(0, 60),
                                            tag: el.tagName,
                                            class: (el.className || '').toString().substring(0, 100),
                                        });
                                    }
                                }
                            } catch(e) {}
                        }
                        return results;
                    }""", config.CAPTCHA_SELECTORS), timeout=2.0)
                    if candidates:
                        frame_info["candidates"] = candidates
                        frame_info["candidate_count"] = len(candidates)
                except Exception as e:
                    frame_info["error"] = str(e)[:100]
                frames_info.append(frame_info)
            except Exception as e:
                frames_info.append({"index": i, "error": str(e)[:100]})

        # 5. 主页面通用属性查找(不含 iframe 内的)
        extra_candidates = []
        try:
            # 注意: Playwright 的 Page.evaluate() 同样不接受 timeout 关键字参数,
            # 用 asyncio.wait_for 包裹。
            extra_candidates = await asyncio.wait_for(page.evaluate("""() => {
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (!el.getBoundingClientRect) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 30 || r.width > 500) continue;
                    if (r.height < 10 || r.height > 100) continue;
                    const cls = (el.className || '').toString().toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    const dataAttrs = Object.values(el.dataset || {}).join(' ').toLowerCase();
                    const haystack = (cls + ' ' + id + ' ' + dataAttrs);
                    if (haystack.includes('captcha') || haystack.includes('slider') ||
                        haystack.includes('verify') || haystack.includes('nc-') ||
                        haystack.includes('geetest') || haystack.includes('slide')) {
                        results.push({
                            tag: el.tagName,
                            class: (el.className || '').toString().substring(0, 80),
                            id: (el.id || '').toString().substring(0, 60),
                            bbox: {x: Math.round(r.x), y: Math.round(r.y),
                                   w: Math.round(r.width), h: Math.round(r.height)},
                            text: (el.textContent || '').trim().substring(0, 40),
                        });
                    }
                }
                return results.slice(0, 20);
            }"""), timeout=3.0)
        except Exception:
            pass

        # 6. 汇总信息并保存
        total_candidates = sum(f.get("candidate_count", 0) for f in frames_info)
        info = {
            "captcha_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "viewport": vp_info,
            "visible_selectors": visible_selectors,
            "frames": frames_info,
            "extra_candidates": extra_candidates,
            "total_frames": len(page.frames),
            "total_candidates_in_frames": total_candidates,
        }
        info_json.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"📋 [诊断] DOM 信息已保存: {info_json}")
        log(f"   CSS视口 {vp_info.get('width',0)}x{vp_info.get('height',0)} (DPR={vp_info.get('dpr',1)})")
        log(f"   {len(page.frames)} 个iframe, frame内{total_candidates}个候选, 主页面{len(extra_candidates)}个候选")

    except Exception as e:
        log(f"⚠️ [诊断] 截图/信息收集异常: {e}")


# ============ 滑块自动滑动: 视觉定位 + 人类化拖动 ============

AUTO_SLIDE_CAPTCHA = True  # 自动尝试滑动滑块(失败后回退到手动模式)
SLIDE_MAX_RETRIES = 3      # 自动滑动最大重试次数
CAPTCHA_FAIL_COOLDOWN = 30  # 自动滑动连续失败后,至少等待 30 秒再重试
_last_captcha_fail_time = 0  # 上次自动滑动失败时间

async def _locate_slider_from_screenshot(screenshot_path: str, dpr: float = 1.0):
    """从截图中定位滑块的手柄和轨道位置, 返回 CSS 像素坐标
    返回 None 表示定位失败"""
    try:
        import cv2
        import numpy as np

        img = cv2.imread(screenshot_path)
        if img is None:
            log("   [debug] 截图读取失败")
            return None
        h, w = img.shape[:2]
        scale = 1.0 / dpr if dpr > 0 else 1.0
        log(f"   [debug] 截图尺寸: {w}x{h}, scale={scale:.3f}")

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        # 1. 先找白色/浅色弹窗
        lower_light = np.array([0, 0, 180])
        upper_light = np.array([180, 50, 255])
        light_mask = cv2.inRange(hsv, lower_light, upper_light)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 20))
        light_closed = cv2.morphologyEx(light_mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(light_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        popup = None
        found_popups = []
        for cnt in sorted(contours, key=cv2.contourArea, reverse=True):
            area = cv2.contourArea(cnt)
            if area > 20000:
                x, y, cw, ch = cv2.boundingRect(cnt)
                center_x = x + cw/2
                found_popups.append((x, y, cw, ch, area))
                log(f"   [debug] 发现浅色区域: 位置({x},{y}), 尺寸{cw}x{ch}, 面积{area:.0f}, 中心({center_x:.0f},{y+ch/2:.0f})")
                if abs(center_x - w/2) < w/3 and popup is None:
                    popup = (x, y, cw, ch)
                    log(f"   [debug] 选定弹窗区域: {popup}")

        if popup is None:
            log("   [debug] 未找到弹窗, 使用默认中心区域")
            cx, cy = w // 2, h // 2
            popup = (cx - 250, cy - 200, 500, 400)

        px, py, pw, ph = popup

        # 2. 裁剪弹窗区域
        roi_x1 = max(0, px + 20)
        roi_y1 = max(0, py + ph // 2)
        roi_x2 = min(w, px + pw - 20)
        roi_y2 = min(h, py + ph - 30)
        roi = img[roi_y1:roi_y2, roi_x1:roi_x2]

        log(f"   [debug] 搜索区域: ({roi_x1},{roi_y1})-({roi_x2},{roi_y2}), 尺寸{roi.shape[1]}x{roi.shape[0]}")

        if roi.size == 0:
            log("   [debug] ROI 为空")
            return None

        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        # 3. 用边缘检测找矩形滑块轨道
        edges = cv2.Canny(roi_gray, 20, 80)
        kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 1))
        edges_dilated = cv2.dilate(edges, kernel_h, iterations=2)
        edges_closed = cv2.morphologyEx(edges_dilated, cv2.MORPH_CLOSE, kernel_h)

        track_contours, _ = cv2.findContours(edges_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        log(f"   [debug] 边缘检测找到 {len(track_contours)} 个轮廓")

        best_track = None
        track_candidates = []
        for cnt in track_contours:
            area = cv2.contourArea(cnt)
            if area < 100:
                continue
            x, y, cw, ch = cv2.boundingRect(cnt)
            aspect = cw / max(ch, 1)
            if cw > 150 and 10 <= ch <= 60 and aspect > 6:
                track_candidates.append((roi_x1 + x, roi_y1 + y, cw, ch, area, aspect))
                log(f"   [debug] 候选轨道: 位置({roi_x1+x},{roi_y1+y}), 尺寸{cw}x{ch}, 宽高比{aspect:.1f}, 面积{area:.0f}")
                if best_track is None or cw * ch > best_track[2] * best_track[3]:
                    best_track = (roi_x1 + x, roi_y1 + y, cw, ch)

        # 备选: 颜色检测
        if best_track is None:
            log("   [debug] 边缘检测未找到轨道, 尝试颜色检测...")
            roi_hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            lower_gray = np.array([0, 0, 150])
            upper_gray = np.array([180, 40, 230])
            gray_mask = cv2.inRange(roi_hsv, lower_gray, upper_gray)
            gray_closed = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, kernel)
            contours2, _ = cv2.findContours(gray_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            for cnt in contours2:
                area = cv2.contourArea(cnt)
                if area < 500:
                    continue
                x, y, cw, ch = cv2.boundingRect(cnt)
                aspect = cw / max(ch, 1)
                if cw > 150 and 10 <= ch <= 60 and aspect > 5:
                    log(f"   [debug] 颜色检测候选轨道: 位置({roi_x1+x},{roi_y1+y}), 尺寸{cw}x{ch}, 宽高比{aspect:.1f}")
                    if best_track is None or cw * ch > best_track[2] * best_track[3]:
                        best_track = (roi_x1 + x, roi_y1 + y, cw, ch)

        if best_track is None:
            log("   [debug] 未找到滑块轨道")
            return None

        track_x, track_y, track_w, track_h = best_track
        log(f"   [debug] 选定轨道: ({track_x},{track_y}), 宽{track_w}, 高{track_h}")

        # 4. 找滑块手柄
        handle_search_x1 = max(0, track_x - 10)
        handle_search_y1 = max(0, track_y - 20)
        handle_search_x2 = min(w, track_x + track_w + 10)
        handle_search_y2 = min(h, track_y + track_h + 20)
        handle_roi = img[handle_search_y1:handle_search_y2, handle_search_x1:handle_search_x2]

        handle_hsv = cv2.cvtColor(handle_roi, cv2.COLOR_BGR2HSV)
        handle_gray = cv2.cvtColor(handle_roi, cv2.COLOR_BGR2GRAY)

        lower_white = np.array([0, 0, 200])
        upper_white = np.array([180, 60, 255])
        white_mask = cv2.inRange(handle_hsv, lower_white, upper_white)
        _, bright_mask = cv2.threshold(handle_gray, 200, 255, cv2.THRESH_BINARY)
        combined_mask = cv2.bitwise_or(white_mask, bright_mask)

        handle_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        combined_closed = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, handle_kernel)
        handle_contours, _ = cv2.findContours(combined_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        track_cy = track_y + track_h / 2
        best_handle = None

        log(f"   [debug] 手柄搜索区域: ({handle_search_x1},{handle_search_y1})-({handle_search_x2},{handle_search_y2})")
        log(f"   [debug] 找到 {len(handle_contours)} 个候选手柄轮廓")

        for cnt in handle_contours:
            area = cv2.contourArea(cnt)
            if area < 100 or area > 5000:
                continue
            hx, hy, hw, hh = cv2.boundingRect(cnt)
            handle_cx = handle_search_x1 + hx + hw / 2
            handle_cy = handle_search_y1 + hy + hh / 2
            cy_diff = abs(handle_cy - track_cy)
            if cy_diff < track_h / 2 + 15:
                log(f"   [debug] 候选手柄: 位置({handle_cx:.0f},{handle_cy:.0f}), 尺寸{hw}x{hh}, 面积{area:.0f}, Y差{cy_diff:.0f}")
                if best_handle is None or area > best_handle[4]:
                    best_handle = (handle_search_x1 + hx, handle_search_y1 + hy, hw, hh, area)

        if best_handle is None:
            log("   [debug] 手柄精确检测失败, 使用轨道左边缘作为手柄位置 (拖到右边解锁类型)")
            # 对于 "拖到右边解锁" 类型的滑块, 手柄就在轨道最左端
            # 估算手柄尺寸约为轨道高度的 1.5 倍
            fallback_hw = track_h * 1.5
            fallback_hh = track_h * 1.5
            handle_cx = track_x + fallback_hw / 2 + 5  # 留一点余量
            handle_cy = track_y + track_h / 2
            result = {
                "handle_x": handle_cx * scale,
                "handle_y": handle_cy * scale,
                "track_width": track_w * scale,
                "track_height": track_h * scale,
                "handle_width": fallback_hw * scale,
                "handle_height": fallback_hh * scale,
                "total_distance": (track_w - fallback_hw) * scale,
                "screenshot_size": {"width": w, "height": h},
                "css_size": {"width": int(w * scale), "height": int(h * scale)},
            }
            log(f"   [debug] 兜底定位: 手柄({result['handle_x']:.0f},{result['handle_y']:.0f}), 移动距离{result['total_distance']:.0f}")
            return result

        hx, hy, hw, hh, _ = best_handle
        handle_cx = hx + hw / 2
        handle_cy = hy + hh / 2

        result = {
            "handle_x": handle_cx * scale,
            "handle_y": handle_cy * scale,
            "track_width": track_w * scale,
            "track_height": track_h * scale,
            "handle_width": hw * scale,
            "handle_height": hh * scale,
            "total_distance": (track_w - hw) * scale,
            "screenshot_size": {"width": w, "height": h},
            "css_size": {"width": int(w * scale), "height": int(h * scale)},
        }
        log(f"   [debug] 定位成功: 手柄({result['handle_x']:.0f},{result['handle_y']:.0f}), 轨道宽{result['track_width']:.0f}, 移动距离{result['total_distance']:.0f}")
        return result

    except Exception as e:
        log(f"   [debug] 定位异常: {e}")
        return None


# ============ Midscene OS 级客户端 ============

async def _calibrate_slider_via_dom(page, loc: dict) -> dict:
    """
    用 DOM 查询校准滑块坐标:
    在页面中查找滑块轨道/手柄元素, 获取精确的 bounding box,
    替换 OpenCV 截图估算的坐标. 特别适用于"拖到右边"类型滑块.
    """
    try:
        # 在页面和 iframe 中查找滑块相关元素
        slider_info = await page.evaluate("""() => {
            const results = [];
            
            // 选择器: 滑块容器/轨道的常见 class 和 role
            const selectors = [
                '.slider', '.captcha-slider', '.drag-slider',
                '.slider-track', '.captcha-track', '.drag-track',
                '[class*="slider"]', '[class*="captcha"]', '[class*="drag"]',
                '[class*="slide"]', '[class*="verify"]', '[class*="check"]',
                'div[class*="bar"]', 'div[class*="track"]',
                '[role="slider"]', '[role="scrollbar"]',
            ];
            
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 100 && rect.height > 5 && rect.height < 80) {
                            // 可能是滑块轨道 (宽>100, 高在5-80之间)
                            results.push({
                                selector: sel,
                                tag: el.tagName,
                                class: el.className.substring(0, 80),
                                x: rect.x, y: rect.y,
                                w: rect.width, h: rect.height,
                                cx: rect.x + rect.width/2,
                                cy: rect.y + rect.height/2,
                                text: (el.textContent || '').trim().substring(0, 50),
                            });
                        }
                    }
                } catch(e) {}
            }
            
            // 同时在所有 iframe 中查找
            const iframes = document.querySelectorAll('iframe');
            const iframeInfo = [];
            for (const iframe of iframes) {
                const irect = iframe.getBoundingClientRect();
                iframeInfo.push({
                    x: irect.x, y: irect.y,
                    w: irect.width, h: irect.height,
                    src: (iframe.src || '').substring(0, 100),
                    id: iframe.id,
                });
            }
            
            return { results, iframeInfo };
        }""")
        
        slider_elements = slider_info.get("results", [])
        iframe_info = slider_info.get("iframeInfo", [])
        
        log(f"   [DOM校准] 找到 {len(slider_elements)} 个滑块候选元素, {len(iframe_info)} 个 iframe")
        
        # 在 iframe 中查找 captcha 滑块
        captcha_iframes = [f for f in iframe_info if "captcha" in (f.get("src", "") or f.get("id", "") or "").lower()]
        
        if captcha_iframes:
            iframe = captcha_iframes[0]
            log(f"   [DOM校准] 检测到 captcha iframe @ ({iframe['x']:.0f},{iframe['y']:.0f}), 尺寸{iframe['w']:.0f}x{iframe['h']:.0f}")
        
        # 用 DOM 元素校准坐标
        if slider_elements:
            # 选择最宽的元素作为滑块轨道
            best = max(slider_elements, key=lambda e: e['w'])
            log(f"   [DOM校准] 最佳轨道: {best['selector']} ({best['w']:.0f}x{best['h']:.0f}) @ ({best['cx']:.0f},{best['cy']:.0f})")
            if best.get('text'):
                log(f"   [DOM校准] 轨道文本: {best['text']}")
            
            # 用 DOM 精确坐标替换 OpenCV 估算
            track_x = best['x']
            track_y = best['y']
            track_w = best['w']
            track_h = best['h']
            
            # 手柄: 轨道左边缘附近 (拖到右边类型)
            handle_w = min(track_h * 2, 50)  # 手柄宽度估算
            handle_x = track_x + handle_w / 2
            handle_y = track_y + track_h / 2
            distance = track_w - handle_w
            
            calibrated = {
                "handle_x": handle_x,
                "handle_y": handle_y,
                "track_width": track_w,
                "track_height": track_h,
                "handle_width": handle_w,
                "handle_height": handle_w,
                "total_distance": distance,
                "dom_calibrated": True,
            }
            log(f"   [DOM校准] 校准后: 手柄({handle_x:.0f},{handle_y:.0f}), 距离{distance:.0f}")
            return calibrated
        
        # 没有找到 DOM 元素, 但有 captcha iframe → 用 iframe 位置修正坐标
        if captcha_iframes:
            iframe = captcha_iframes[0]
            log(f"   [DOM校准] 未找到滑块 DOM, 用 iframe 位置修正")
            # iframe 内容相对 iframe 左上定位, 需要加上 iframe 偏移
            # OpenCV 返回的坐标是页面截图坐标, 已经包含 iframe 偏移
            # 所以这里保持原值, 但增加验证日志
            log(f"   [DOM校准] OpenCV 坐标(未经 DOM 校准): handle=({loc['handle_x']:.0f},{loc['handle_y']:.0f})")
        
        return loc
        
    except Exception as e:
        log(f"   [DOM校准] 异常: {e}, 使用原始坐标")
        return loc


async def _midscene_health_check() -> bool:
    """检查 Midscene 服务是否可用"""
    if not config.MIDSCENE_ENABLED:
        return False
    try:
        import urllib.request
        url = f"{config.MIDSCENE_BASE_URL}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            return data.get("status") == "ok"
    except Exception:
        return False


async def _midscene_locate_slider(prompt: str = "") -> Optional[dict]:
    """
    调用 Midscene 视觉定位滑块
    返回: {"handle": {"x", "y"}, "gap": {"x", "y"}} 或 None
    """
    if not config.MIDSCENE_ENABLED:
        return None
    try:
        import urllib.request
        payload = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(
            f"{config.MIDSCENE_BASE_URL}/locate_slider",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if data.get("success"):
                log(f"   [Midscene] 视觉定位成功: handle={data['handle']}, gap={data.get('gap')}")
                return {"handle": data["handle"], "gap": data.get("gap")}
            else:
                log(f"   [Midscene] 视觉定位失败: {data.get('error')}")
                return None
    except Exception as e:
        log(f"   [Midscene] 调用异常: {e}")
        return None


async def _midscene_perform_drag(points: list, start_delay_ms: int = 0, end_delay_ms: int = 0) -> bool:
    """
    调用 Midscene OS 级拖拽
    points: [{"x", "y", "delayMs"}, ...]
    """
    if not config.MIDSCENE_ENABLED:
        return False
    try:
        import urllib.request
        payload = json.dumps({
            "points": points,
            "startDelayMs": start_delay_ms,
            "endDelayMs": end_delay_ms
        }).encode()
        req = urllib.request.Request(
            f"{config.MIDSCENE_BASE_URL}/perform_drag",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data.get("success", False)
    except Exception as e:
        log(f"   [Midscene] 拖拽调用异常: {e}")
        return False


async def _midscene_ai_solve_slider(prompt: str = "") -> bool:
    """
    用 Midscene aiAct 直接让 AI 视觉理解+执行滑块拖拽 (一步到位)
    带详细日志: 发送什么、等待什么、收到什么
    """
    if not config.MIDSCENE_ENABLED:
        return False
    if not await _midscene_health_check():
        return False
    
    import uuid
    session_id = f"py_{uuid.uuid4().hex[:12]}"
    
    try:
        import urllib.request
        import time
        
        # 构建请求
        payload = json.dumps({
            "prompt": prompt,
            "sessionId": session_id,
        }).encode()
        
        url = f"{config.MIDSCENE_BASE_URL}/ai_solve_slider"
        
        log(f"   [Midscene AI] {'='*50}")
        log(f"   [Midscene AI] 📤 发送 HTTP POST 请求")
        log(f"   [Midscene AI]    URL: {url}")
        log(f"   [Midscene AI]    Session: {session_id}")
        log(f"   [Midscene AI]    Payload ({len(payload)} bytes): {json.dumps({'prompt': prompt[:80]+'...', 'sessionId': session_id})}")
        log(f"   [Midscene AI]    Timeout: 180s")
        log(f"   [Midscene AI] {'='*50}")
        
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        
        log(f"   [Midscene AI] ⏳ 等待 Midscene 响应 (可能 30-60s, AI 需要截图+分析+执行)...")
        t0 = time.time()
        
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode()
            elapsed = time.time() - t0
            status = resp.status
            
        log(f"   [Midscene AI] 📥 收到响应: HTTP {status} ({elapsed:.1f}s)")
        
        # 解析响应
        data = json.loads(raw)
        
        success = data.get("success", False)
        method = data.get("method", "unknown")
        debug = data.get("debug", {})
        
        log(f"   [Midscene AI] 📊 响应摘要:")
        log(f"   [Midscene AI]    success: {success}")
        log(f"   [Midscene AI]    method: {method}")
        
        if success:
            log(f"   [Midscene AI] ✅ 成功!")
        else:
            log(f"   [Midscene AI] ❌ 失败!")
            log(f"   [Midscene AI]    error: {data.get('error', '未知')}")
            if data.get('fallbackError'):
                log(f"   [Midscene AI]    fallbackError: {data['fallbackError']}")
        
        # 打印详细 debug 信息
        if debug:
            total_ms = debug.get("totalDurationMs", 0)
            log(f"   [Midscene AI] 📋 执行步骤 ({total_ms}ms 总计):")
            for step in debug.get("steps", []):
                step_name = step.get("name", "?")
                step_dur = step.get("durationMs", 0)
                log(f"   [Midscene AI]      [{step_name}] {step_dur}ms")
            
            screenshots = debug.get("screenshots", [])
            if screenshots:
                log(f"   [Midscene AI] 📸 截图文件 ({len(screenshots)} 张):")
                for s in screenshots:
                    stage = s.get("stage", "?")
                    path = s.get("path", "?")
                    log(f"   [Midscene AI]      [{stage}] {path}")
        
        # 额外打印 aiAct 返回值摘要
        if data.get("result"):
            result_str = json.dumps(data["result"], ensure_ascii=False)[:500]
            log(f"   [Midscene AI] 🤖 AI 返回: {result_str}")
        
        return success
        
    except Exception as e:
        log(f"   [Midscene AI] ❌ 调用异常: {e} (session={session_id})")
        import traceback
        log(f"   [Midscene AI]    Traceback: {traceback.format_exc()[-300:]}")
        return False


def _build_drag_trajectory(handle_x: float, handle_y: float, distance: float, 
                           screen_offset_x: float = 0, screen_offset_y: float = 0) -> list:
    """
    构建物理仿真拖拽轨迹(复用现有 _human_drag_slider 的算法)
    返回 Midscene 需要的 points 数组
    
    screen_offset: 浏览器窗口左上角在屏幕上的偏移量(macOS 需要)
    """
    total_distance = distance
    screen_x = handle_x + screen_offset_x
    screen_y = handle_y + screen_offset_y
    
    points = []
    
    # 1. 起点(带随机偏移)
    offset_x = random.uniform(-3, 3)
    offset_y = random.uniform(-3, 3)
    target_x = screen_x + offset_x
    target_y = screen_y + offset_y
    
    # 起点
    points.append({"x": target_x - random.uniform(5, 15), "y": target_y, "delayMs": 0})
    points.append({"x": target_x, "y": target_y, "delayMs": 50})
    
    # 2. 加速 -> 匀速 -> 减速
    current_x = target_x
    current_y = target_y
    steps_count = max(int(total_distance / random.uniform(4, 8)), 20)
    
    for i in range(steps_count):
        t = i / steps_count
        velocity = 1.0 - abs(2 * t - 1) ** 2  # 钟形速度曲线
        step_distance = (total_distance / steps_count) * (0.5 + velocity * 0.8)
        step_distance += random.uniform(-0.5, 0.5)
        y_drift = random.uniform(-1.5, 1.5)
        
        current_x += step_distance
        current_y = screen_y + y_drift
        
        points.append({
            "x": current_x,
            "y": current_y,
            "delayMs": int(8 + random.uniform(-3, 5))
        })
    
    # 3. 过冲
    overshoot = random.uniform(2, 4)
    points.append({
        "x": current_x + overshoot,
        "y": current_y + random.uniform(-1, 1),
        "delayMs": int(30 + random.uniform(0, 50))
    })
    
    # 4. 回调
    points.append({
        "x": current_x - overshoot / 2,
        "y": current_y + random.uniform(-1, 1),
        "delayMs": int(50 + random.uniform(0, 50))
    })
    
    # 5. 最终位置
    final_x = screen_x + total_distance
    final_y = screen_y + random.uniform(-1, 1)
    points.append({"x": final_x, "y": final_y, "delayMs": int(150 + random.uniform(0, 150))})
    
    return points


async def _human_drag_slider_os(page, handle_x: float, handle_y: float, distance: float):
    """
    使用 Midscene OS 级拖拽滑块(路线 A)
    需要 Midscene Node.js 服务运行中
    """
    if not config.MIDSCENE_ENABLED:
        log("   [Midscene] 未启用,回退到 Playwright 拖拽")
        return await _human_drag_slider(page, handle_x, handle_y, distance)
    
    # 1. 获取浏览器窗口在屏幕上的位置(OS 级坐标需要)
    try:
        win_info = await page.evaluate("""() => ({
            screenX: window.screenX || window.screenLeft || 0,
            screenY: window.screenY || window.screenTop || 0,
            outerWidth: window.outerWidth,
            outerHeight: window.outerHeight,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            dpr: window.devicePixelRatio || 1,
        })""")
        # macOS Chrome: window.screenX/Y 表示窗口左上角的屏幕坐标
        screen_offset_x = win_info.get("screenX", 0) + (win_info.get("outerWidth", 0) - win_info.get("innerWidth", 0)) // 2
        screen_offset_y = win_info.get("screenY", 0) + (win_info.get("outerHeight", 0) - win_info.get("innerHeight", 0)) - 1
        dpr = win_info.get("dpr", 1)
        log(f"   [Midscene] 窗口偏移: ({screen_offset_x}, {screen_offset_y}), DPR={dpr}")
    except Exception as e:
        log(f"   [Midscene] 获取窗口位置失败: {e}, 使用估算值")
        screen_offset_x = 0
        screen_offset_y = 0
    
    # 2. 等待 Midscene 服务
    if not await _midscene_health_check():
        log("   [Midscene] 服务不可用,回退到 Playwright 拖拽")
        return await _human_drag_slider(page, handle_x, handle_y, distance)
    
    # 3. 构建轨迹(CSS 坐标 + 窗口偏移 = 屏幕坐标)
    points = _build_drag_trajectory(handle_x, handle_y, distance, screen_offset_x, screen_offset_y)
    log(f"   [Midscene] 轨迹: {len(points)} 个点")
    
    # 4. OS 级拖拽
    # 按下前停顿:模拟"鼠标移到滑块 → 思考 → 按下"
    start_delay = int(120 + random.uniform(0, 200))
    # 松开后停顿:模拟"确认验证结果"
    end_delay = int(150 + random.uniform(0, 200))
    
    success = await _midscene_perform_drag(points, start_delay, end_delay)
    
    if success:
        log(f"   [Midscene] OS 级拖拽执行完成")
    else:
        log(f"   [Midscene] OS 级拖拽失败,回退到 Playwright")
        return await _human_drag_slider(page, handle_x, handle_y, distance)
    
    return success


async def _human_drag_slider(page, handle_x: float, handle_y: float, distance: float):
    """用 page.mouse 人类化地拖动滑块, 返回是否成功"""
    total_distance = distance  # 需要拖动的总距离( CSS 像素)

    # 1. 移动到滑块位置(带随机偏移)
    offset_x = random.uniform(-3, 3)
    offset_y = random.uniform(-3, 3)
    target_x = handle_x + offset_x
    target_y = handle_y + offset_y

    # 先快速移到附近, 再微调
    await page.mouse.move(target_x - random.uniform(5, 15), target_y, steps=10)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.move(target_x, target_y, steps=5)
    await asyncio.sleep(random.uniform(0.2, 0.4))  # 反应时间

    # 2. 按下鼠标
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.1, 0.2))  # 按下后短暂停顿

    # 3. 分段拖动: 加速 -> 匀速 -> 减速
    current_x = target_x
    current_y = target_y
    steps_count = max(int(total_distance / random.uniform(4, 8)), 20)
    time_per_step = 0.008  # 每步 8ms

    for i in range(steps_count):
        t = i / steps_count
        # 速度曲线: 钟形(sin 曲线), 两端慢, 中间快
        velocity = 1.0 - abs(2 * t - 1) ** 2  # 0~1~0 的钟形
        step_distance = (total_distance / steps_count) * (0.5 + velocity * 0.8)
        step_distance += random.uniform(-0.5, 0.5)  # 微小抖动

        # Y 轴随机漂移(模拟手的不稳定)
        y_drift = random.uniform(-1.5, 1.5)

        current_x += step_distance
        current_y += y_drift

        await page.mouse.move(current_x, current_y, steps=1)
        await asyncio.sleep(time_per_step + random.uniform(-0.003, 0.005))

    # 4. 过冲: 稍微拖过一点再拉回(模拟真人过冲回调)
    overshoot = random.uniform(2, 4)
    await page.mouse.move(current_x + overshoot, current_y + random.uniform(-1, 1), steps=3)
    await asyncio.sleep(random.uniform(0.03, 0.08))

    # 回调
    await page.mouse.move(current_x - overshoot / 2, current_y + random.uniform(-1, 1), steps=3)
    await asyncio.sleep(random.uniform(0.05, 0.1))

    # 最终位置
    final_x = handle_x + total_distance
    final_y = handle_y + random.uniform(-1, 1)
    await page.mouse.move(final_x, final_y, steps=5)
    await asyncio.sleep(random.uniform(0.15, 0.3))

    # 5. 抬起鼠标
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.2, 0.4))

    return True


def _locate_slider_by_template(screenshot_path: str, dpr: float = 1.0):
    """图像相似度(模板匹配)定位滑块手柄, 并推算拖拽距离。
    在 CDP 视口截图上用 cv2.matchTemplate(多尺度)找 >> 手柄按钮,
    再沿手柄所在行向右扫描浅灰轨道求右端。返回 CSS 坐标 dict 或 None。
    """
    try:
        import cv2
        import numpy as np
    except Exception as e:
        log(f"   [模板] 缺少 cv2/numpy: {e}")
        return None

    scale = 1.0 / dpr if dpr > 0 else 1.0
    img = cv2.imread(screenshot_path)
    if img is None:
        log("   [模板] 截图读取失败")
        return None
    tpl = cv2.imread(config.SLIDER_TEMPLATE_PATH)
    if tpl is None:
        log(f"   [模板] 模板读取失败: {config.SLIDER_TEMPLATE_PATH}")
        return None

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape[:2]
    th0, tw0 = tpl_gray.shape[:2]

    # 多尺度匹配(应对 DPR / 弹窗尺寸差异)
    best = None  # (score, cx, cy, tw, th)
    for s in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.6, 2.0]:
        tw, th = int(tw0 * s), int(th0 * s)
        if tw < 12 or th < 8 or tw >= W or th >= H:
            continue
        resized = cv2.resize(tpl_gray, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if best is None or max_val > best[0]:
            best = (max_val, max_loc[0] + tw / 2, max_loc[1] + th / 2, tw, th)

    if best is None:
        log("   [模板] 无有效尺度")
        return None

    score, hcx, hcy, tw, th = best
    log(f"   [模板] 最佳匹配 score={score:.3f} 手柄中心(px)=({hcx:.0f},{hcy:.0f}) 模板尺寸={tw}x{th}")
    if score < config.SLIDER_TEMPLATE_THRESHOLD:
        return None

    # 沿手柄所在行向右扫描浅灰轨道, 求右端 (轨道≈灰, 背景≈白)
    band_y1 = max(0, int(hcy - th * 0.35))
    band_y2 = min(H, int(hcy + th * 0.35))
    col_med = np.median(gray[band_y1:band_y2, :], axis=0)  # 每列灰度中位数
    start_x = int(hcx + tw * 0.5)
    track_right = start_x
    gap = 0
    x = start_x
    while x < W:
        v = col_med[x]
        if v < 248:              # 非纯白 → 视为轨道(含轨道内文字)
            track_right = x
            gap = 0
        else:
            gap += 1
            if gap > int(tw * 0.6):   # 连续白到一定宽度, 认为轨道结束
                break
        x += 1

    handle_x_css = hcx * scale
    handle_y_css = hcy * scale
    # 拖到轨道右端(留一点余量, _human_drag_slider 内部还会 overshoot)
    distance_px = (track_right - hcx) - tw * 0.3
    distance_css = distance_px * scale
    # 距离兜底/封顶: 太小(轨道没扫到)用手柄宽度的若干倍; 太大(扫进了弹窗外的暗色遮罩)封顶
    if distance_css < tw * scale:
        distance_css = max(tw * scale * 5, 200)
        log(f"   [模板] 轨道右端不可靠, 用兜底距离 {distance_css:.0f}")
    distance_css = min(distance_css, tw * scale * 8)

    # 可视化: 在定位时刻的截图上标注(红框=手柄, 绿线=拖拽目标/轨道右端), 供人工核对
    try:
        vis = img.copy()
        x1, y1 = int(hcx - tw / 2), int(hcy - th / 2)
        x2, y2 = int(hcx + tw / 2), int(hcy + th / 2)
        cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 3)          # 红框: 手柄(应落在左侧白色椭圆)
        cv2.circle(vis, (int(hcx), int(hcy)), 4, (0, 0, 255), -1)        # 红点: 手柄中心(拖拽起点)
        target_px = int(hcx + distance_css / scale)                      # 拖拽终点 x(按最终距离)
        cv2.line(vis, (target_px, y1 - 10), (target_px, y2 + 10), (0, 200, 0), 2)  # 绿线: 目标右端
        cv2.arrowedLine(vis, (int(hcx), int(hcy)), (target_px, int(hcy)), (0, 200, 0), 2, tipLength=0.03)
        cv2.putText(vis, f"score={score:.2f} dist={distance_css:.0f}css",
                    (x1, max(0, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        vis_path = str(config.RAW_DUMP_DIR / f"captcha_locate_{time.strftime('%Y%m%d_%H%M%S')}.png")
        cv2.imwrite(vis_path, vis)
        log(f"   [模板] 🖍️ 定位标注图已保存: {vis_path} (红框=手柄, 绿箭头=拖拽方向)")
    except Exception as e:
        log(f"   [模板] 标注图保存失败: {e}")

    result = {
        "handle_x": handle_x_css,
        "handle_y": handle_y_css,
        "total_distance": distance_css,
        "score": float(score),
    }
    log(f"   [模板] 定位: 手柄({handle_x_css:.0f},{handle_y_css:.0f}) 轨道右端px={track_right} 距离{distance_css:.0f}(CSS)")
    return result


async def _try_image_slide(page) -> bool:
    """图像定位 + Playwright 拖拽(全程浏览器内, 不用 DOM / OS 鼠标 / 显示器)。
    先轮询"截图+模板匹配"直到定位到手柄(解决弹窗渲染时序问题), 再人类化拖拽。
    成功返回 True; 失败返回 False。
    """
    config.RAW_DUMP_DIR.mkdir(parents=True, exist_ok=True)

    # DPR
    try:
        dpr = (await page.evaluate("() => window.devicePixelRatio || 1")) or 1
    except Exception:
        dpr = 1

    # 1) 轮询: 截图 → 模板匹配, 直到定位到手柄(或超时)
    loc = None
    deadline = time.time() + config.SLIDER_LOCATE_MAX_WAIT
    shot = str(config.RAW_DUMP_DIR / f"captcha_tpl_{time.strftime('%Y%m%d_%H%M%S')}.png")
    log(f"   [图像] 轮询截图+模板匹配 (最长 {config.SLIDER_LOCATE_MAX_WAIT:.0f}s, DPR={dpr})...")
    while time.time() < deadline:
        try:
            await page.screenshot(path=shot, full_page=False)
        except Exception as e:
            log(f"   [图像] 截图失败: {e}")
            await asyncio.sleep(config.SLIDER_LOCATE_POLL)
            continue
        loc = _locate_slider_by_template(shot, dpr)
        if loc is not None:
            break
        await asyncio.sleep(config.SLIDER_LOCATE_POLL)

    if loc is None:
        # 兜底: 用现有 OpenCV 轨道检测(同样在视口截图上)
        try:
            await page.screenshot(path=shot, full_page=False)
            loc = await _locate_slider_from_screenshot(shot, dpr)
        except Exception:
            loc = None
    if loc is None:
        log("   [图像] 未定位到滑块, 放弃")
        return False

    # 2) 用 Playwright page.mouse 人类化拖拽 (浏览器内, 不碰 OS 鼠标)
    for attempt in range(config.PLAYWRIGHT_SLIDE_MAX_RETRIES):
        hx = loc["handle_x"]
        hy = loc["handle_y"]
        dist = loc.get("total_distance") or loc.get("distance") or 200
        log(f"   [图像] 第{attempt + 1}/{config.PLAYWRIGHT_SLIDE_MAX_RETRIES}次拖拽: 手柄({hx:.0f},{hy:.0f}) 距离{dist:.0f}px")
        try:
            await _human_drag_slider(page, hx, hy, dist)
        except Exception as e:
            log(f"   [图像] 拖拽异常: {e}")
            await asyncio.sleep(0.5)

        await asyncio.sleep(1.2)

        # 滑块是否消失
        still_visible = False
        for sel in config.CAPTCHA_SELECTORS:
            try:
                locator = page.locator(sel).first
                if await locator.count() > 0 and await locator.is_visible(timeout=200):
                    still_visible = True
                    break
            except Exception:
                pass
        if not still_visible:
            log(f"   [图像] ✅ 滑块已通过! (第{attempt + 1}次)")
            return True

        log(f"   [图像] ⚠️ 滑块仍在, 重新定位重试...")
        await asyncio.sleep(0.4)
        try:
            await page.screenshot(path=shot, full_page=False)
            new_loc = _locate_slider_by_template(shot, dpr)
            if new_loc is not None:
                loc = new_loc
        except Exception:
            pass

    log("   [图像] ❌ 拖拽未通过")
    return False


async def _try_auto_slide_captcha(page):
    """尝试自动滑动滑块, 返回是否成功"""
    global _last_captcha_fail_time
    if not AUTO_SLIDE_CAPTCHA:
        return False

    # 失败冷却: 上次失败后短时间内不重试, 防止刷屏
    if _last_captcha_fail_time and (time.time() - _last_captcha_fail_time) < CAPTCHA_FAIL_COOLDOWN:
        remaining = int(CAPTCHA_FAIL_COOLDOWN - (time.time() - _last_captcha_fail_time))
        log(f"   ⏳ 自动滑动冷却中, {remaining}s 后可重试 (等待期间请手动完成)")
        return False

    log("🤖 尝试自动滑动滑块...")

    # 0. 优先: 图像定位(模板匹配) + Playwright 拖拽 (浏览器内, 不碰 OS 鼠标/显示器)
    if config.PREFER_PLAYWRIGHT_SLIDE:
        if await _try_image_slide(page):
            return True
        if not config.USE_MIDSCENE_SLIDE_FALLBACK:
            log("   图像方案失败, Midscene 已禁用(USE_MIDSCENE_SLIDE_FALLBACK=False), 请手动完成")
            _last_captcha_fail_time = time.time()
            return False
        log("   回退: 尝试 Midscene 视觉方案...")

    # 1. 获取 DPR 和 CSS 视口尺寸
    try:
        info = await page.evaluate("""() => ({
            dpr: window.devicePixelRatio || 1,
            innerW: window.innerWidth || 0,
            innerH: window.innerHeight || 0,
        })""")
        dpr = info.get("dpr", 1)
        css_w = info.get("innerW", 0)
        css_h = info.get("innerH", 0)
    except Exception:
        dpr = 1
        css_w, css_h = 1280, 800

    # 2. 截图用于视觉定位
    ts = time.strftime("%Y%m%d_%H%M%S")
    config.RAW_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = str(config.RAW_DUMP_DIR / f"captcha_auto_{ts}.png")
    try:
        await page.screenshot(path=screenshot_path, full_page=False)
        log(f"   截图: {screenshot_path}")
    except Exception:
        log("   ⚠️ 截图失败")
        return False

    # 3. 自动拖动 (优先 aiAct, 失败后回退到 locate + 手动拖拽)
    SLIDER_AI_PROMPT = (
        '找到滑块验证组件中的手柄（通常在滑轨左侧的起点，带有 >> 箭头图标或其他可拖拽标识），'
        '按住手柄沿滑轨向右拖动，直到到达滑轨最右端完成验证'
    )
    
    # 3a. 优先尝试 aiAct (AI 视觉一步解决, 不需要坐标)
    if config.MIDSCENE_ENABLED and await _midscene_health_check():
        log(f"   [Midscene] 🤖 优先尝试 aiAct (AI 视觉拖拽, 一步到位)...")
        log(f"   [Midscene AI] prompt: {SLIDER_AI_PROMPT}")
        ai_success = await _midscene_ai_solve_slider(prompt=SLIDER_AI_PROMPT)
        if ai_success:
            log(f"   [Midscene AI] ✅ aiAct 拖拽成功!")
            return True
        log(f"   [Midscene AI] aiAct 失败, 回退到坐标定位方案...")
    
    # 3b. 视觉定位 (aiAct 失败后的回退)
    loc = None
    
    # 3b-1. 尝试 Midscene OS 级视觉定位
    if config.MIDSCENE_ENABLED and await _midscene_health_check():
        log(f"   [Midscene] 尝试 AI 视觉定位 (locate)...")
        midscene_result = await _midscene_locate_slider(
            '屏幕上有一个滑块验证区域,请找到滑块手柄的位置和目标缺口位置。滑块通常在屏幕下方或弹窗内。'
        )
        if midscene_result and midscene_result.get("handle"):
            h = midscene_result["handle"]
            g = midscene_result.get("gap")
            # h 现在一定是 {x, y} dict 格式 (server.js 已归一化)
            loc = {
                "handle_x": h["x"],
                "handle_y": h["y"],
                "total_distance": abs(g["x"] - h["x"]) if g else 200,
                "track_width": abs(g["x"] - h["x"]) if g else 300,
            }
            gap_str = f", gap=({g['x']:.0f},{g['y']:.0f})" if g else ""
            log(f"   [Midscene] 定位结果: handle=({h['x']:.0f},{h['y']:.0f})"
                f"{gap_str}, 距离={loc['total_distance']:.0f}")
        else:
            log(f"   [Midscene] locate 失败, 回退到 OpenCV")
    
    # 3b-2. 回退: OpenCV 截图定位
    if loc is None:
        log(f"   分析截图(DPR={dpr}, CSS视口{css_w}x{css_h})...")
        loc = await _locate_slider_from_screenshot(screenshot_path, dpr)
        if loc is None:
            log("   ⚠️ 视觉定位失败, 无法继续 (aiAct 和 locate 都失败了)")
            _last_captcha_fail_time = time.time()
            return False

        # 3b-3. DOM 校准
        loc = await _calibrate_slider_via_dom(page, loc)
        log(f"   定位结果: 手柄({loc['handle_x']:.0f},{loc['handle_y']:.0f})"
            f", 轨道宽{loc['track_width']:.0f}, 需拖动{loc['total_distance']:.0f}像素")

    # 4. 用坐标执行拖拽 (OS 级或 Playwright)
    for attempt in range(SLIDE_MAX_RETRIES):
        log(f"   尝试 {attempt + 1}/{SLIDE_MAX_RETRIES} (坐标拖拽)...")
        try:
            if config.MIDSCENE_ENABLED and await _midscene_health_check():
                log(f"   [Midscene] OS 级拖拽: ({loc['handle_x']:.0f},{loc['handle_y']:.0f}) → 拖 {loc['total_distance']:.0f}px")
                success = await _human_drag_slider_os(
                        page,
                        loc["handle_x"],
                        loc["handle_y"],
                        loc["total_distance"]
                    )
            else:
                log(f"   [Playwright] Midscene 未就绪, 使用 Playwright 拖拽...")
                success = await _human_drag_slider(
                    page,
                    loc["handle_x"],
                    loc["handle_y"],
                    loc["total_distance"]
                )

            # 等待验证结果
            await asyncio.sleep(1.0)

            # 检查滑块是否还存在(如果成功验证, 滑块应该消失)
            still_visible = False
            for sel in config.CAPTCHA_SELECTORS:
                try:
                    locator = page.locator(sel).first
                    if await locator.count() > 0 and await locator.is_visible(timeout=200):
                        still_visible = True
                        break
                except Exception:
                    pass

            if not still_visible:
                log(f"   ✅ 自动滑动成功! (第{attempt + 1}次尝试)")
                return True
            elif not success:
                log(f"   ⚠️ 拖拽执行失败, 重试...")
                await asyncio.sleep(0.3)
            else:
                log(f"   ⚠️ 滑块仍然可见, 重试...")
                await asyncio.sleep(0.3)
                # 重新截图定位(可能滑块位置变了)
                if attempt < SLIDE_MAX_RETRIES - 1:
                    await page.screenshot(path=screenshot_path, full_page=False)
                    new_loc = await _locate_slider_from_screenshot(screenshot_path, dpr)
                    if new_loc:
                        loc = new_loc
                        log(f"   重新定位: 需拖动{loc['total_distance']:.0f}像素")

        except Exception as e:
            log(f"   ⚠️ 拖动异常: {e}")
            await asyncio.sleep(0.5)

    log(f"   ❌ 自动滑动失败({SLIDE_MAX_RETRIES}次尝试), 请手动完成")
    _last_captcha_fail_time = time.time()  # 记录失败时间, 触发冷却
    return False


_last_mouse_jitter = 0
def _human_keystroke_delay_ms(prev_char: str, curr_char: str) -> int:
    import random, math
    # 对数正态分布：中位数 ~53ms, σ=0.30
    base = random.lognormvariate(math.log(0.053), 0.30) * 1000
    # 标点前减速（当前字符是标点）
    if curr_char in "，。！？；：、,.!?;:":
        base *= 1.6
    # 句末长停顿（前一个字符是句末标点）
    if prev_char in "。！？!?":
        base += random.uniform(200, 550)
    # 生理下限 35ms
    return max(int(base), 35)

async def mouse_jitter(page):
    global _last_mouse_jitter
    import random
    now = time.time()
    if now - _last_mouse_jitter < 3.5:
        return
    _last_mouse_jitter = now
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        cx = random.randint(int(viewport["width"] * 0.15), int(viewport["width"] * 0.85))
        cy = random.randint(int(viewport["height"] * 0.2), int(viewport["height"] * 0.8))
        steps = random.randint(3, 6)
        await page.mouse.move(cx, cy, steps=steps)
    except Exception:
        pass
