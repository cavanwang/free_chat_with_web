# -*- coding: utf-8 -*-
"""页面操作：模型/对话模式切换、会话重置、新建对话、Midscene 启停。"""
import asyncio
import json
import random
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config
from .state import log, _app_state
from .browser import wait_for_login_then_chat


async def ensure_modes(page, mode_names):
    log(f"🎛️  检查模式: {mode_names}")
    check_js = """(el) => {
        if (!el) return false;
        const p = el.getAttribute('aria-pressed');
        const c = el.getAttribute('aria-checked');
        if (p === 'true' || c === 'true') return true;
        const cls = ((el.className||'')+' '+(el.parentElement&&el.parentElement.className||'')).toLowerCase();
        return ['active','selected','checked','-on','enable','primary'].some(k=>cls.includes(k));
    }"""
    for name in mode_names:
        try:
            chip = None
            for sel in [f'button:has-text("{name}")', f'[role="button"]:has-text("{name}")',
                        f'div[role="switch"]:has-text("{name}")', f'span:has-text("{name}")']:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                        chip = loc
                        break
                except Exception:
                    continue
            if chip:
                handle = await chip.element_handle()
                if await page.evaluate(check_js, handle):
                    log(f"   ✅ 「{name}」已开启")
                else:
                    log(f"   🔘 开启「{name}」...")
                    await chip.click()
                    await page.wait_for_timeout(800)
                    h2 = await chip.element_handle()
                    on = await page.evaluate(check_js, h2)
                    log(f"   {'✅' if on else '⚠️'} 「{name}」→ {'已开启' if on else '可能未开启'}")
            else:
                log(f"   ⚠️ 未找到「{name}」独立按钮，尝试从对话模式下拉菜单切换...")
                if await switch_chat_mode(page, name):
                    log(f"   ✅ 「{name}」通过对话模式切换成功")
                else:
                    log(f"   ❌ 「{name}」在页面中未找到")
        except Exception as e:
            log(f"   ❌ 「{name}」出错: {e}")
    log("🎛️  模式设置完成")


MODEL_BLACKLIST_KEYWORDS = [
    "办公助理", "PPT", "AI生成", "AI作图", "AI图", "本地电脑",
    "更多", "联网搜索", "深度搜索", "深度研究", "思考研究",
    "快速", "扫码", "下载", "注册", "登录",
]


def _is_valid_model_name(name: str) -> bool:
    if not name or len(name) > 60:
        return False
    for kw in MODEL_BLACKLIST_KEYWORDS:
        if kw in name:
            return False
    return True


MODEL_NAME_PATTERN = re.compile(r"^Qwen", re.IGNORECASE)


async def _validate_model_trigger(locator):
    try:
        full_text = (await locator.inner_text(timeout=1000)).strip()
        log(f"      [validate] full_text=「{full_text}」")
        if MODEL_NAME_PATTERN.match(full_text):
            return True
    except Exception as e:
        log(f"      [validate] inner_text异常: {e}")

    try:
        inner = locator.locator(".text-primary").first
        cnt = await inner.count()
        log(f"      [validate] .text-primary count={cnt}")
        if cnt > 0:
            text = (await inner.inner_text(timeout=1000)).strip()
            log(f"      [validate] .text-primary text=「{text}」")
            if MODEL_NAME_PATTERN.match(text):
                return True
    except Exception as e:
        log(f"      [validate] .text-primary异常: {e}")

    return False


async def _validate_chat_mode_trigger(locator):
    try:
        text = (await locator.inner_text(timeout=1000)).strip()
        log(f"      [validate_chat_mode] text=「{text}」")
        mode_keywords = ["快速", "思考", "思考研究"]
        for kw in mode_keywords:
            if kw in text:
                return True
    except Exception as e:
        log(f"      [validate_chat_mode] inner_text异常: {e}")
    return False


async def _diagnose_page_structure(page):
    log("   🔍 诊断页面结构...")
    try:
        diag_js = """() => {
            const results = {};
            results.totalButtons = document.querySelectorAll('button').length;
            results.typeButtons = document.querySelectorAll('button[type="button"]').length;
            results.hasPopupDialog = document.querySelectorAll('[aria-haspopup="dialog"]').length;
            results.hasPopupDialogButton = document.querySelectorAll('button[aria-haspopup="dialog"]').length;
            results.hasPopupDialogTypeButton = document.querySelectorAll('button[type="button"][aria-haspopup="dialog"]').length;
            results.hasControlsRadix = document.querySelectorAll('[aria-controls^="radix"]').length;
            results.hasControlsRadixButton = document.querySelectorAll('button[aria-controls^="radix"]').length;
            results.hasTextPrimary = document.querySelectorAll('.text-primary').length;
            results.qwenTextPrimary = Array.from(document.querySelectorAll('.text-primary'))
                .filter(el => el.textContent.trim().startsWith('Qwen')).length;
            results.textPrimarySamples = Array.from(document.querySelectorAll('.text-primary'))
                .slice(0, 10).map(el => el.textContent.trim());
            
            const iframes = document.querySelectorAll('iframe');
            results.iframeCount = iframes.length;
            results.iframeDetails = Array.from(iframes).map(f => ({
                src: f.src ? f.src.substring(0, 100) : '(no src)',
                id: f.id || '',
                className: f.className || ''
            }));
            
            results.bodyClasses = document.body ? document.body.className.substring(0, 200) : '';
            
            return results;
        }"""
        result = await page.evaluate(diag_js)
        log(f"     button 总数: {result.get('totalButtons', 'N/A')}")
        log(f"     type=button: {result.get('typeButtons', 'N/A')}")
        log(f"     aria-haspopup=dialog: {result.get('hasPopupDialog', 'N/A')}")
        log(f"     button+aria-haspopup=dialog: {result.get('hasPopupDialogButton', 'N/A')}")
        log(f"     button[type=button]+aria-haspopup=dialog: {result.get('hasPopupDialogTypeButton', 'N/A')}")
        log(f"     aria-controls^=radix: {result.get('hasControlsRadix', 'N/A')}")
        log(f"     button+aria-controls^=radix: {result.get('hasControlsRadixButton', 'N/A')}")
        log(f"     .text-primary: {result.get('hasTextPrimary', 'N/A')}")
        log(f"     .text-primary 以Qwen开头: {result.get('qwenTextPrimary', 'N/A')}")
        log(f"     .text-primary 样例: {result.get('textPrimarySamples', [])}")
        log(f"     iframe 数量: {result.get('iframeCount', 'N/A')}")
        if result.get('iframeDetails'):
            for d in result.get('iframeDetails', []):
                log(f"       iframe: id={d.get('id')} class={d.get('className')} src={d.get('src', '')[:80]}")
        return result
    except Exception as e:
        log(f"     诊断异常: {e}")
        return {}


async def _open_and_get_panel(page, trigger_selectors, panel_selectors, validate_fn=None):
    trigger = None
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel).first
            cnt = await loc.count()
            log(f"   [selector] {sel} → count={cnt}")
            if cnt > 0:
                visible = await loc.is_visible(timeout=2000)
                log(f"   [selector] visible={visible}")
                if not visible:
                    continue
                if validate_fn:
                    passed = await validate_fn(loc)
                    log(f"   [selector] validate={passed}")
                    if not passed:
                        continue
                text = ""
                try:
                    text = await loc.inner_text(timeout=500)
                except Exception:
                    pass
                controls_id = await loc.get_attribute("aria-controls") or ""
                log(f"   触发器匹配: {sel} → 文本:「{text.strip()}」 aria-controls={controls_id}")
                trigger = loc
                break
        except Exception as e:
            log(f"   [selector] {sel} → 异常: {e}")
            continue
    if not trigger:
        await _diagnose_page_structure(page)
        return None, None

    expanded_before = await trigger.get_attribute("aria-expanded") or "false"
    log(f"   点击前 aria-expanded={expanded_before}")

    if expanded_before == "true":
        log("   面板已展开，跳过点击")
    else:
        # 点击前的认知停顿(模拟"找到按钮→移动鼠标→决定点击")
        await page.wait_for_timeout(random.randint(120, 320))
        log("   尝试点击打开面板...")
        clicked = False
        for attempt in range(3):
            try:
                await trigger.click()
                clicked = True
                break
            except Exception:
                try:
                    await trigger.evaluate("el => el.click()")
                    clicked = True
                    break
                except Exception:
                    await page.wait_for_timeout(random.randint(400, 800))
        
        if not clicked:
            log("   ❌ 无法点击按钮")
            return None, None

    # 点击后面板展开的等待(动画 + 渲染)
    await page.wait_for_timeout(random.randint(400, 800))

    expanded_after_click = await trigger.get_attribute("aria-expanded") or "false"
    log(f"   点击后 aria-expanded={expanded_after_click}")

    if expanded_after_click != "true":
        log("   面板未展开，等待后重试...")
        await page.wait_for_timeout(random.randint(600, 1000))
        expanded_after_click = await trigger.get_attribute("aria-expanded") or "false"
        log(f"   重试后 aria-expanded={expanded_after_click}")

    panel = None
    controls_id = await trigger.get_attribute("aria-controls") or ""
    log(f"   aria-controls={controls_id}")

    if controls_id:
        try:
            escaped_id = controls_id.replace(":", "\\:")
            loc = page.locator(f'#{escaped_id}').first
            if await loc.count() > 0:
                visible = await loc.is_visible(timeout=2000)
                log(f"   #{controls_id} 可见={visible}")
                if visible:
                    inner_len = await loc.evaluate("el => el.innerHTML.length")
                    log(f"   #{controls_id} innerHTML长度={inner_len}")
                    if inner_len > 100:
                        log(f"   面板匹配: #{controls_id}")
                        panel = loc
                    else:
                        log(f"   #{controls_id} 内容过少，可能是占位符")
        except Exception as e:
            log(f"   #{controls_id} 定位异常: {e}")

    if not panel:
        for psel in panel_selectors:
            try:
                loc = page.locator(psel).first
                if await loc.count() > 0 and await loc.is_visible(timeout=2000):
                    inner_len = await loc.evaluate("el => el.innerHTML.length")
                    log(f"   面板匹配: {psel} innerHTML长度={inner_len}")
                    if inner_len > 100:
                        panel = loc
                        break
            except Exception:
                continue

    if not panel and controls_id:
        try:
            loc = page.locator(f'#{controls_id}').first
            if await loc.count() > 0:
                log(f"   面板匹配(无转义): #{controls_id}")
                panel = loc
        except Exception:
            pass

    if not panel:
        try:
            loc = page.locator('[data-state="open"]').first
            if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                inner_len = await loc.evaluate("el => el.innerHTML.length")
                log(f"   [data-state=open] innerHTML长度={inner_len}")
                if inner_len > 100:
                    log(f"   面板匹配: [data-state=open]")
                    panel = loc
        except Exception:
            pass

    if not panel:
        try:
            all_dialogs = page.locator('[role="dialog"]')
            for i in range(await all_dialogs.count()):
                d = all_dialogs.nth(i)
                if await d.is_visible(timeout=500):
                    inner_len = await d.evaluate("el => el.innerHTML.length")
                    log(f"   [role=dialog] #{i} innerHTML长度={inner_len}")
                    if inner_len > 100:
                        log(f"   面板匹配: [role=dialog] #{i}")
                        panel = d
                        break
        except Exception:
            pass

    if not panel:
        log("   ⚠️ 未能定位展开的面板")
        return trigger, None

    return trigger, panel


async def _extract_items_from_panel(page, panel, item_selectors, blacklist_fn):
    results = []
    seen = set()
    for sel in item_selectors:
        try:
            items = panel.locator(sel)
            count = await items.count()
            if count > 0:
                log(f"   面板内选择器命中 {count} 个: {sel}")
                for i in range(count):
                    try:
                        text = (await items.nth(i).inner_text(timeout=1000)).strip()
                        if not text:
                            continue
                        text = _clean_model_name(text)
                        if text in seen:
                            continue
                        if blacklist_fn(text):
                            seen.add(text)
                            results.append(text)
                        else:
                            log(f"   过滤掉: 「{text}」")
                    except Exception:
                        continue
                if results:
                    break
                else:
                    log(f"   选择器 {sel} 全部被过滤，尝试下一个...")
        except Exception:
            continue

    if not results:
        log("   Playwright选择器未命中，改用JS直接提取菜单项...")
        try:
            js_result = await panel.evaluate("""el => {
                const itemEls = el.querySelectorAll(
                    '[role="menuitemcheckbox"], [role="menuitemradio"], [role="menuitem"], [data-radix-collection-item]'
                );
                const items = [];
                const seen = new Set();
                for (const it of itemEls) {
                    let primary = null;
                    const primarySpans = it.querySelectorAll(
                        '[class*="text-primary"]:not([class*="text-caption"])'
                    );
                    for (const s of primarySpans) {
                        const t = s.textContent.trim();
                        if (t) { primary = t; break; }
                    }
                    if (!primary) {
                        const full = (it.innerText || it.textContent || '').trim();
                        primary = full.split('\\n')[0].trim();
                    }
                    if (primary && primary.length >= 2 && primary.length < 30 && !seen.has(primary)) {
                        seen.add(primary);
                        items.push(primary);
                    }
                }
                return { items, total: itemEls.length };
            }""")
            log(f"   JS匹配到 {len(js_result.get('items', []))} 个(菜单项总数={js_result.get('total')}):")
            for name in js_result.get("items", []):
                if blacklist_fn(name):
                    results.append(name)
                else:
                    log(f"   过滤掉: 「{name}」")
        except Exception as e:
            log(f"   JS提取异常: {e}")

    return results


def _clean_model_name(text: str) -> str:
    text = text.split('\n')[0].strip()
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'(新模型|默认|设为默认|推荐)$', '', text).strip()
    text = re.sub(r'[，,].*$', '', text).strip()
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


async def list_models(page):
    log("📋 获取模型列表...")
    trigger, panel = await _open_and_get_panel(page, config.MODEL_TRIGGER_SELECTORS, config.MODEL_PANEL_SELECTORS, validate_fn=_validate_model_trigger)

    if not trigger:
        log("   ⚠️ 未找到模型切换按钮")
        return []
    if not panel:
        log("   ⚠️ 面板未展开")
        return []

    models = await _extract_items_from_panel(page, panel, config.MODEL_ITEM_SELECTORS, _is_valid_model_name)

    # 关闭面板前停顿(模拟"看完列表→决定关闭"的认知过程)
    await page.wait_for_timeout(random.randint(250, 550))

    # 优先: 点击已选中的菜单项(radix 标准关闭方式, 比 Escape 可靠)
    closed = False
    try:
        # radix menuitemcheckbox/menuitemradio: 选中项带 data-state="checked"
        checked = panel.locator('[data-state="checked"]').first
        if await checked.count() > 0 and await checked.is_visible(timeout=1000):
            await checked.click()
            closed = True
    except Exception:
        pass

    if not closed:
        # 次选: 点击触发器(radix 菜单触发器二次点击会关闭面板)
        try:
            await trigger.click()
            closed = True
        except Exception:
            pass

    if not closed:
        # 兜底: Escape
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # 等待关闭动画完成
    await page.wait_for_timeout(random.randint(300, 600))

    if models:
        log(f"   发现 {len(models)} 个模型:")
        for m in models:
            log(f"   - {m}")
    else:
        log("   ⚠️ 未能获取模型列表")
    return models


async def switch_model(page, model_name):
    log(f"🔄 切换模型 → 「{model_name}」")
    trigger, panel = await _open_and_get_panel(page, config.MODEL_TRIGGER_SELECTORS, config.MODEL_PANEL_SELECTORS, validate_fn=_validate_model_trigger)

    if not trigger:
        log("   ❌ 未找到模型切换按钮")
        return False
    if not panel:
        log("   ❌ 面板未展开")
        return False

    try:
        exact = panel.locator(f'div[class*="truncate"]:has-text("{model_name}")').first
        if await exact.count() > 0 and await exact.is_visible(timeout=2000):
            await exact.click()
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        exact2 = panel.get_by_text(model_name, exact=True).first
        if await exact2.count() > 0 and await exact2.is_visible(timeout=2000):
            await exact2.click()
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        fuzzy = panel.get_by_text(model_name, exact=False).first
        if await fuzzy.count() > 0 and await fuzzy.is_visible(timeout=2000):
            await fuzzy.click()
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        fallback_js = f"""el => {{
            if (!el) return false;
            const selectors = ['div[class*="truncate"]', '[role="option"]', '[role="menuitem"]', 'button', '[class*="item"]'];
            for (const sel of selectors) {{
                const els = el.querySelectorAll(sel);
                for (const el2 of els) {{
                    const t = el2.textContent.trim();
                    const cleaned = t.replace(/\\s+/g, ' ').replace(/(新模型|默认|设为默认)$/g, '').trim();
                    if (cleaned === '{model_name}' || cleaned.includes('{model_name}')) {{
                        el2.click();
                        return true;
                    }}
                }}
            }}
            return false;
        }}"""
        result = await panel.evaluate(fallback_js)
        if result:
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    log(f"   ❌ 未找到模型「{model_name}」")
    return False


def _is_valid_chat_mode_name(name: str) -> bool:
    if not name or len(name) > 60:
        return False
    blacklist = ["办公助理", "PPT", "AI生成", "AI作图", "AI图", "本地电脑", "更多", "扫码", "下载", "注册", "登录"]
    for kw in blacklist:
        if kw in name:
            return False
    return True


async def list_chat_modes(page):
    log("📋 获取对话模式列表...")
    trigger, panel = await _open_and_get_panel(page, config.CHAT_MODE_TRIGGER_SELECTORS, config.MODEL_PANEL_SELECTORS, validate_fn=_validate_chat_mode_trigger)

    if not trigger:
        log("   ⚠️ 未找到对话模式切换按钮")
        return []
    if not panel:
        log("   ⚠️ 面板未展开")
        return []

    modes = await _extract_items_from_panel(page, panel, config.CHAT_MODE_ITEM_SELECTORS, _is_valid_chat_mode_name)

    # 关闭面板前停顿(模拟"看完列表→决定关闭"的认知过程)
    await page.wait_for_timeout(random.randint(250, 550))

    # 优先: 点击已选中的菜单项(radix 标准关闭方式)
    closed = False
    try:
        checked = panel.locator('[data-state="checked"]').first
        if await checked.count() > 0 and await checked.is_visible(timeout=1000):
            await checked.click()
            closed = True
    except Exception:
        pass

    if not closed:
        try:
            await trigger.click()
            closed = True
        except Exception:
            pass

    if not closed:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # 等待关闭动画完成
    await page.wait_for_timeout(random.randint(300, 600))

    if modes:
        log(f"   发现 {len(modes)} 个对话模式:")
        for m in modes:
            log(f"   - {m}")
    else:
        log("   ⚠️ 未能获取对话模式列表")
    return modes


async def switch_chat_mode(page, mode_name):
    log(f"🔄 切换对话模式 → 「{mode_name}」")
    trigger, panel = await _open_and_get_panel(page, config.CHAT_MODE_TRIGGER_SELECTORS, config.MODEL_PANEL_SELECTORS, validate_fn=_validate_chat_mode_trigger)

    if not trigger:
        log("   ❌ 未找到对话模式切换按钮")
        return False
    if not panel:
        log("   ❌ 面板未展开")
        return False

    # 读取选项前的视觉停顿(模拟"扫一眼列表→定位目标")
    await page.wait_for_timeout(random.randint(180, 450))

    async def _try_click_option(loc) -> bool:
        """尝试点击一个选项,带人类化时序"""
        if await loc.count() > 0 and await loc.is_visible(timeout=2000):
            # 点击前的认知停顿(模拟"决定点击这个")
            await page.wait_for_timeout(random.randint(150, 400))
            await loc.click()
            # 切换模式涉及 UI 变化,等待更长
            await page.wait_for_timeout(random.randint(600, 1200))
            return True
        return False

    # 1. 精确选择器(radix 菜单项)
    try:
        exact = panel.locator(f'[role="menuitemcheckbox"]:has-text("{mode_name}")').first
        if await _try_click_option(exact):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 2. 其他菜单项角色
    try:
        exact_alt = panel.locator(f'[role="menuitem"]:has-text("{mode_name}"), [role="menuitemradio"]:has-text("{mode_name}")').first
        if await _try_click_option(exact_alt):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 3. 精确文本匹配
    try:
        exact2 = panel.get_by_text(mode_name, exact=True).first
        if await _try_click_option(exact2):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 4. 模糊文本匹配
    try:
        fuzzy = panel.get_by_text(mode_name, exact=False).first
        if await _try_click_option(fuzzy):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 5. JS fallback(更新选择器,优先 menuitemcheckbox)
    try:
        fallback_js = f"""(panelEl) => {{
            if (!panelEl) return false;
            const selectors = [
                '[role="menuitemcheckbox"]', '[role="menuitemradio"]',
                '[role="menuitem"]', '[data-radix-collection-item]',
                '[role="option"]', 'button', '[class*="item"]'
            ];
            for (const sel of selectors) {{
                const els = panelEl.querySelectorAll(sel);
                for (const el of els) {{
                    const t = (el.textContent || '').trim();
                    if (t === '{mode_name}' || t.startsWith('{mode_name}')) {{
                        el.click();
                        return true;
                    }}
                }}
            }}
            return false;
        }}"""
        result = await page.evaluate(fallback_js, panel)
        if result:
            await page.wait_for_timeout(random.randint(600, 1200))
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 关闭面板前停顿
    await page.wait_for_timeout(random.randint(200, 450))
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(random.randint(300, 600))
    log(f"   ❌ 未找到对话模式「{mode_name}」")
    return False


async def reset_session(new_page=True):
    """重置会话: 关闭旧标签页, 新建标签页, 返回新的 page"""
    page = _app_state["page"]
    context = _app_state["context"]
    browser = _app_state["browser"]

    if not page or not context or not browser:
        log("❌ 浏览器未初始化, 无法重置会话")
        return page

    log("🔄 重置会话...")

    # 1. 关闭旧标签页
    try:
        if new_page:
            await page.close()
            log("   已关闭旧标签页")
    except Exception as e:
        log(f"   ⚠️ 关闭旧标签页异常: {e}")

    # 2. 新建标签页
    try:
        new_page = await context.new_page()
        log("   已新建标签页")
    except Exception as e:
        log(f"   ❌ 新建标签页失败: {e}")
        return page

    # 3. 导航到千问
    try:
        await new_page.goto(config.QWEN_URL, wait_until="domcontentloaded")
        await new_page.wait_for_timeout(3000)
        log("   已导航到千问首页")
    except Exception as e:
        log(f"   ⚠️ 导航异常: {e}")

    # 4. 注入 stealth 补丁
    try:
        await new_page.add_init_script(config.STEALTH_PATCH_JS)
    except Exception:
        pass
    try:
        await new_page.evaluate(config.STEALTH_PATCH_JS)
    except Exception:
        pass

    # 5. 等待登录
    if not await wait_for_login_then_chat(new_page, config.LOGIN_TIMEOUT_SEC):
        log("❌ 新标签页登录超时")
        return page

    # 6. 确保模式
    await ensure_modes(new_page, config.ENABLE_MODES)
    if config.DEFAULT_MODEL:
        await switch_model(new_page, config.DEFAULT_MODEL)

    # 7. 更新状态
    _app_state["page"] = new_page
    log("✅ 会话已重置")
    return new_page


async def _click_center(page, el):
    """移动到元素中心并点击一次(模拟鼠标落点), 失败回退普通 click。"""
    try:
        box = await el.bounding_box()
        if not box:
            await el.click(timeout=2000)
            return True
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await page.mouse.move(cx, cy, steps=random.randint(5, 10))
        await page.wait_for_timeout(random.randint(60, 140))
        await page.mouse.click(cx, cy)
        return True
    except Exception:
        try:
            await el.click(timeout=2000)
            return True
        except Exception:
            return False


async def _wait_input_ready(page, timeout_ms=6000):
    """等待输入框出现(新会话就绪的标志)。"""
    steps = max(1, timeout_ms // 400)
    for _ in range(steps):
        for sel in config.INPUT_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=400):
                    return True
            except Exception:
                continue
        await page.wait_for_timeout(200)
    return False


def _looks_new_chat_url(before, after):
    """用 URL 变化判断是否真的开了新会话。
    - after 不含 /chat/ (回到基础页/新会话页) -> 视为新会话
    - after 与点击前的 /chat/{id} 不同 -> 视为切到了新会话
    这可把"新建对话加号"和"侧栏收起/展开切换"区分开(后者不改 URL)。
    """
    if not after:
        return True
    if "/chat/" not in after:
        return True
    return after != before


async def _click_top_plus_button(page):
    """收起态兜底: 在顶部区域找到"加号"图标按钮并点击。
    通过点击前后 URL 是否变化来确认确实开了新会话, 避免误点侧栏开关。
    """
    try:
        btns = page.locator('button:has(svg)')
        n = await btns.count()
    except Exception:
        return False
    candidates = []
    for i in range(min(n, 25)):
        el = btns.nth(i)
        try:
            if not await el.is_visible(timeout=200):
                continue
            box = await el.bounding_box()
            if not box:
                continue
            if box["y"] <= 140:  # 只看顶部一行的按钮
                candidates.append((box["x"], el))
        except Exception:
            continue
    if not candidates:
        return False
    # 顶部按钮按从左到右排序; 最左通常是"侧栏开关", 加号一般紧随其后。
    # 逐个尝试点击, 用 URL 变化确认, 命中即止。
    candidates.sort(key=lambda c: c[0])
    for _x, el in candidates:
        before = ""
        try:
            before = page.url
        except Exception:
            pass
        if not await _click_center(page, el):
            continue
        await _wait_input_ready(page, timeout_ms=3000)
        after = ""
        try:
            after = page.url
        except Exception:
            pass
        if _looks_new_chat_url(before, after):
            return True
    return False


async def start_new_chat(page):
    """点击"新建对话/加号"按钮开启一个干净会话。
    - 侧栏展开: 命中带文字/无障碍属性的按钮;
    - 侧栏收起: 命中顶部加号图标按钮(结构兜底, 以 URL 变化确认)。
    成功返回可用的 page(与入参相同, 同标签页内新建);
    全部失败则回退 reset_session(关/开标签页), 返回新的 page。
    """
    # 1) 优先: 具名(aria-label/title/文字)按钮。
    #    这些按钮语义明确, 点击+输入框就绪即视为成功(不要求 URL 变化:
    #    部分 SPA 在发首条消息前不改 URL, 强求会误触发重的 reset 兜底)。
    named = [s for s in config.NEW_CHAT_SELECTORS if s != 'button:has(svg)']
    for sel in named:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                if await _click_center(page, el):
                    if await _wait_input_ready(page):
                        log(f"🆕 已新建会话(按钮: {sel})")
                        return page
        except Exception:
            continue

    # 2) 结构兜底: 顶部加号图标按钮
    try:
        if await _click_top_plus_button(page):
            log("🆕 已新建会话(顶部加号按钮)")
            return page
    except Exception:
        pass

    # 3) 兜底: 关/开标签页重置(必定得到干净会话)
    log("⚠️ 未点到新建对话按钮, 回退 reset_session 重置会话")
    new_page = await reset_session(new_page=True)
    return new_page if new_page else page


def _prompt_midscene_enabled():
    """交互式询问是否启用 Midscene OS 级操作
    
    如果已通过命令行或环境变量确定,则跳过询问
    """
    
    # 如果已通过命令行或环境变量确定,跳过交互式询问
    if config._MIDSCENE_EXTERNAL_SET:
        status = "启用" if config.MIDSCENE_ENABLED else "未启用"
        log(f"   Midscene: {status} (已通过外部配置确定)")
        if config.MIDSCENE_ENABLED:
            _verify_midscene_service()
        return
    
    print()
    print("=" * 50)
    print("  Midscene OS 级自动化(路线 A)")
    print("  基于视觉识别 + OS 级键鼠操作的反检测方案")
    print("=" * 50)
    print()
    print("  启用后,滑块验证将走 Midscene 路径:")
    print("    1. AI 视觉定位滑块(零 DOM 痕迹)")
    print("    2. OS 级键鼠拖拽(最高反检测强度)")
    print()
    print("  前提条件:")
    print("    • Midscene Node.js 服务已启动(端口 3456)")
    print("    • macOS 辅助功能权限已授权")
    print("=" * 50)
    
    while True:
        try:
            choice = input("\n  是否启用 Midscene? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            config.MIDSCENE_ENABLED = False
            log("   未启用 Midscene")
            return
        
        if choice in ('y', 'yes'):
            config.MIDSCENE_ENABLED = True
            log("   ✅ 已启用 Midscene OS 级操作")
            
            # 可选:自定义 Midscene 服务地址
            try:
                url = input(f"   Midscene 服务地址 [默认 {config.MIDSCENE_BASE_URL}]: ").strip()
                if url:
                    config.MIDSCENE_BASE_URL = url
            except (EOFError, KeyboardInterrupt):
                pass
            
            _verify_midscene_service()
            return
        elif choice in ('', 'n', 'no'):
            config.MIDSCENE_ENABLED = False
            log("   未启用 Midscene,滑块将使用 Playwright 处理")
            return
        else:
            print("   请输入 y 或 n")


def _verify_midscene_service():
    """验证 Midscene 服务连通性"""
    
    try:
        import urllib.request
        with urllib.request.urlopen(f"{config.MIDSCENE_BASE_URL}/health", timeout=2) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "ok" and data.get("agentInitialized"):
                log(f"   ✅ Midscene 服务就绪 (agent 已初始化)")
            elif data.get("status") == "ok":
                log(f"   ⚠️  Midscene 服务在线,但 agent 未初始化")
                log(f"      首次调用时会自动初始化(可能需要几秒)")
            else:
                log(f"   ⚠️  Midscene 服务状态异常: {data}")
    except Exception:
        log(f"   ⚠️  无法连接到 Midscene 服务 ({config.MIDSCENE_BASE_URL})")
        log(f"      请先启动: cd midscene-computer && ./start.sh")
        log(f"      当前将继续运行,但滑块会回退到 Playwright 处理")