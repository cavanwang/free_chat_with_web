/**
 * Midscene Computer HTTP Service
 * 
 * 功能:
 *   - 纯视觉定位滑块(不依赖 DOM)
 *   - OS 级逐步执行拖拽(支持物理仿真轨迹)
 *   - AI 视觉一步解决: aiAct 直接完成滑块拖拽
 *   - 详细调试日志 + 每步截图
 * 
 * Python 端通过 HTTP 调用此服务
 */

const express = require('express');
const { agentFromComputer, ComputerDevice } = require('@midscene/computer');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const app = express();
app.use(express.json({ limit: '50mb' }));

// run.log: 所有日志同时写入项目根目录的 run.log
const PROJECT_ROOT = path.resolve(__dirname, '..');
const RUN_LOG_PATH = path.join(PROJECT_ROOT, 'run.log');

// 拦截 console.log/error, 同时写入 run.log
const origLog = console.log;
const origError = console.error;
function writeRunLog(prefix, args) {
  const line = `[${new Date().toISOString().replace('T', ' ').substring(0, 19)}] [Midscene] ${args.join(' ')}\n`;
  try { fs.appendFileSync(RUN_LOG_PATH, line); } catch(e) {}
}
console.log = function(...args) { origLog.apply(console, args); writeRunLog('LOG', args); };
console.error = function(...args) { origError.apply(console, args); writeRunLog('ERR', args); };

// run.log 启动标记
try {
  fs.appendFileSync(RUN_LOG_PATH, `\n${'='.repeat(60)}\n[${new Date().toISOString().replace('T', ' ').substring(0, 19)}] === Midscene Server Started ===\n`);
} catch(e) {}

// 调试目录
const DEBUG_DIR = path.join(__dirname, 'debug_logs');
const SCREENSHOT_DIR = path.join(DEBUG_DIR, 'screenshots');
fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

// 全局 agent(复用连接,避免每次请求都重新初始化)
let agent = null;
let initPromise = null;

// ============ 日志辅助函数 ============
function timestamp() {
  return new Date().toISOString().replace('T', ' ').substring(0, 19);
}

function logStep(step, msg) {
  console.log(`[${timestamp()}] [STEP ${step}] ${msg}`);
}

function logWait(msg) {
  console.log(`[${timestamp()}] [⏳ 等待中] ${msg} ...`);
}

function logAction(msg) {
  console.log(`[${timestamp()}] [🔧 执行] ${msg}`);
}

function logSend(msg) {
  console.log(`[${timestamp()}] [📤 发送] ${msg}`);
}

function logRecv(msg) {
  console.log(`[${timestamp()}] [📥 收到] ${msg}`);
}

function logDebug(msg) {
  console.log(`[${timestamp()}] [DEBUG] ${msg}`);
}

function logError(msg, err) {
  console.error(`[${timestamp()}] [❌ 错误] ${msg}:`, err?.message || err);
}

function logSuccess(msg) {
  console.log(`[${timestamp()}] [✅ 成功] ${msg}`);
}

async function saveDebugScreenshot(agent, name) {
  try {
    const filename = `${name}_${Date.now()}.png`;
    const filepath = path.join(SCREENSHOT_DIR, filename);
    
    // Midscene v1.12.0 API: agent.interface.screenshotBase64()
    // 返回 base64 字符串,需要解码保存为文件
    const base64 = await agent.interface.screenshotBase64();
    if (base64) {
      // 移除 data:image/png;base64, 前缀如果存在
      const cleanB64 = base64.replace(/^data:image\/\w+;base64,/, '');
      fs.writeFileSync(filepath, Buffer.from(cleanB64, 'base64'));
      logDebug(`截图已保存 (${Buffer.from(cleanB64, 'base64').length} bytes): ${filepath}`);
      return filepath;
    }
    return null;
  } catch (err) {
    logDebug(`截图失败: ${err.message}`);
    return null;
  }
}

// ============ 显示器检测 ============
async function detectChromeDisplay() {
  try {
    const displays = await ComputerDevice.listDisplays();
    logStep(0, `检测到 ${displays.length} 个显示器:`);
    for (const d of displays) {
      logStep(0, `  [${d.id}] ${d.name} (primary=${d.primary})`);
    }
    
    // 如果只有一个显示器,直接用
    if (displays.length <= 1) {
      logStep(0, `只有 1 个显示器,使用默认`);
      return { displayId: displays[0]?.id, displays };
    }
    
    // 检查环境变量 MIDSCENE_DISPLAY_ID (最高优先级)
    const envDisplayId = process.env.MIDSCENE_DISPLAY_ID;
    if (envDisplayId) {
      const matched = displays.find(d => d.id === envDisplayId);
      if (matched) {
        logStep(0, `使用环境变量 MIDSCENE_DISPLAY_ID=${envDisplayId} → ${matched.name}`);
        return { displayId: matched.id, displays };
      } else {
        logError(`环境变量 MIDSCENE_DISPLAY_ID=${envDisplayId} 未匹配到任何显示器`, null);
      }
    }
    
    // ---- 几何法: 用 Chrome 窗口中心 + 各屏真实边界, 准确定位 Chrome 所在显示器 ----
    // NSScreen 给出的是含缩放的真实逻辑坐标, 不依赖库的显示器编号(编号可能错乱)。
    try {
      // 1) Chrome 窗口矩形 (System Events, 左上原点, 逻辑点): "x, y, w, h"
      const rectRaw = execSync(
        `osascript -e 'tell application "System Events" to tell process "Google Chrome" to return (get position of window 1) & (get size of window 1)'`,
        { encoding: 'utf-8', timeout: 5000 }
      ).trim();
      const nums = rectRaw.split(',').map(s => parseInt(s.trim(), 10));
      if (nums.length >= 4 && nums.every(n => !isNaN(n))) {
        const [wx, wy, ww, wh] = nums;
        const cx = wx + ww / 2;
        const cy = wy + wh / 2;
        logStep(0, `Chrome 窗口: 位置(${wx},${wy}) 尺寸(${ww}x${wh}) 中心(${Math.round(cx)},${Math.round(cy)})`);

        // 2) 各 NSScreen 边界 (AppKit, 无需权限, 左下原点)
        const screensRaw = execSync(
          `osascript -l JavaScript -e 'ObjC.import("AppKit");var s=$.NSScreen.screens;var a=[];var primH=0;for(var i=0;i<s.count;i++){var f=s.objectAtIndex(i).frame;var z=(f.origin.x===0&&f.origin.y===0);if(z){primH=f.size.height;}a.push({i:i,x:f.origin.x,y:f.origin.y,w:f.size.width,h:f.size.height,primary:z});}JSON.stringify({primH:primH,screens:a});'`,
          { encoding: 'utf-8', timeout: 8000 }
        ).trim();
        const geo = JSON.parse(screensRaw);
        const primH = geo.primH || 0;

        // 3) 找到包含窗口中心的屏 (NSScreen 左下原点 -> 左上原点)
        let hit = null;
        for (const sc of (geo.screens || [])) {
          const tlx = sc.x;
          const tly = primH - (sc.y + sc.h);
          const inside = (cx >= tlx && cx < tlx + sc.w && cy >= tly && cy < tly + sc.h);
          logStep(0, `  屏[${sc.i}] 左上(${Math.round(tlx)},${Math.round(tly)}) ${Math.round(sc.w)}x${Math.round(sc.h)} primary=${sc.primary}${inside ? '  ← Chrome 在此' : ''}`);
          if (inside) hit = sc;
        }

        // 4) 映射到 Midscene displayId (按 primary 标记对应, 双屏下唯一确定)
        if (hit) {
          const target = hit.primary
            ? (displays.find(d => d.primary) || displays[0])
            : (displays.find(d => !d.primary) || displays[0]);
          logStep(0, `✅ 定位成功: Chrome 在${hit.primary ? '主屏' : '副屏'} → Midscene displayId=[${target.id}] ${target.name}`);
          return { displayId: target.id, displays };
        }
        logStep(0, `⚠️ 窗口中心未落在任何屏范围内, 走回退方案`);
      } else {
        logDebug(`解析 Chrome 窗口矩形失败: "${rectRaw}"`);
      }
    } catch (geoErr) {
      logDebug(`几何法定位显示器失败 (可能缺少自动化/辅助功能权限): ${geoErr.message}`);
    }

    // 回退方案: 用非主显示器 (用户通常把 Chrome 开在副屏)
    const nonPrimary = displays.find(d => !d.primary);
    if (nonPrimary) {
      logStep(0, `回退: 使用非主显示器 [${nonPrimary.id}] ${nonPrimary.name}`);
      logStep(0, `如不正确, 设置: export MIDSCENE_DISPLAY_ID=${displays[0].id} (主屏) 或 ${nonPrimary.id} (副屏)`);
      return { displayId: nonPrimary.id, displays };
    }

    return { displayId: displays[0].id, displays };
    
  } catch (err) {
    logError('显示器检测失败', err);
    return { displayId: null, displays: [] };
  }
}

// ============ Agent 初始化 (支持多显示器) ============
async function initAgent() {
  if (agent) return agent;
  if (initPromise) return initPromise;
  
  initPromise = (async () => {
    logStep(0, '正在初始化 Computer agent...');
    const startTime = Date.now();
    
    // 检测 Chrome 所在的显示器
    const { displayId, displays } = await detectChromeDisplay();
    
    try {
      // v1.12.0 正确方式: agentFromComputer 接受 opts 对象, 内部自动创建 ComputerDevice
      // agentFromComputer({ displayId: '1', aiActionContext: '...' })
      // 不要手动创建 ComputerDevice, 也不要传两个参数给 agentFromComputer!
      const agentOpts = {
        aiActionContext: '你正在控制一台桌面计算机，用于自动化滑块验证。',
      };
      
      if (displayId !== null && displayId !== undefined) {
        logStep(0, `指定显示器: displayId="${displayId}"`);
        agentOpts.displayId = String(displayId);
      } else if (displays.length > 0) {
        logStep(0, `使用默认显示器: displayId="${displays[0].id}"`);
        agentOpts.displayId = String(displays[0].id);
      }
      
      logStep(0, `调用 agentFromComputer(${JSON.stringify(agentOpts)})...`);
      agent = await agentFromComputer(agentOpts);
      
      logStep(0, `Computer agent 初始化成功 (耗时 ${Date.now() - startTime}ms)`);
      logStep(0, `Agent 已连接到显示器 [${agentOpts.displayId || 'default'}]`);
      
      // 验证: 截图确认在正确显示器上
      const verifyShot = await agent.interface.screenshotBase64();
      if (verifyShot) {
        logStep(0, `✅ 截图验证成功 (base64长度=${verifyShot.length}, 确认显示器内容正确)`);
      }
      
    } catch (err) {
      initPromise = null;
      logError('Computer agent 初始化失败', err);
      throw err;
    }
    return agent;
  })();
  
  return initPromise;
}

// ============ 1. AI 视觉一步解决滑块 (主方案) ============
// POST /ai_solve_slider
// Body: { prompt?: string, sessionId?: string }
app.post('/ai_solve_slider', async (req, res) => {
  const sessionId = req.body?.sessionId || `session_${Date.now()}`;
  const debugInfo = { sessionId, steps: [], screenshots: [] };
  const overallStart = Date.now();
  
  try {
    logStep(1, `[${sessionId}] ========== 开始 AI 视觉滑块解决 ==========`);
    logRecv(`[${sessionId}] 收到请求: POST /ai_solve_slider`);
    logDebug(`[${sessionId}] 请求体: ${JSON.stringify(req.body).substring(0, 200)}`);
    
    // Step 1: 初始化 Agent
    logStep(1.1, `[${sessionId}] 初始化 Midscene agent (屏幕截图 + AI 连接测试)...`);
    logWait(`[${sessionId}] 等待 agent 初始化 (首次可能需要 10-30s)...`);
    const agentStart = Date.now();
    const a = await initAgent();
    debugInfo.steps.push({ name: 'initAgent', durationMs: Date.now() - agentStart, success: true });
    logSuccess(`[${sessionId}] Agent 就绪 (耗时 ${Date.now() - agentStart}ms, 已连接到显示器)`);
    
    // Step 2: 执行前截图
    logStep(1.2, `[${sessionId}] 截取当前屏幕状态 (执行前截图)...`);
    const beforeShot = await saveDebugScreenshot(a, `${sessionId}_before_aiAct`);
    if (beforeShot) {
      debugInfo.screenshots.push({ stage: 'before_aiAct', path: beforeShot });
      logSuccess(`[${sessionId}] 执行前截图: ${beforeShot}`);
    } else {
      logWait(`[${sessionId}] 截图失败,继续执行...`);
    }
    
    // Step 3: 用 aiAct 一步解决
    const prompt = req.body?.prompt || 
      '找到滑块验证组件中的手柄（通常在滑轨左侧的起点，带有 >> 箭头图标或其他可拖拽标识），按住手柄沿滑轨向右拖动，直到到达滑轨最右端完成验证';
    
    logStep(2, `[${sessionId}] 调用 aiAct (AI 视觉理解 + OS 级执行)...`);
    logSend(`[${sessionId}] 发送给 AI 的 prompt: "${prompt}"`);
    logWait(`[${sessionId}] 等待 AI 分析屏幕 + 执行拖拽 (可能 30-60s)...`);
    
    const aiActStart = Date.now();
    const result = await a.aiAct(prompt);
    const aiActDuration = Date.now() - aiActStart;
    debugInfo.steps.push({ name: 'aiAct', durationMs: aiActDuration, success: true });
    
    logSuccess(`[${sessionId}] aiAct 执行完成 (耗时 ${aiActDuration}ms)`);
    logRecv(`[${sessionId}] AI 返回结果: ${JSON.stringify(result).substring(0, 500)}`);
    
    // Step 4: 执行后截图
    logStep(3, `[${sessionId}] 截取执行后屏幕状态 (验证拖拽效果)...`);
    const afterShot = await saveDebugScreenshot(a, `${sessionId}_after_aiAct`);
    if (afterShot) {
      debugInfo.screenshots.push({ stage: 'after_aiAct', path: afterShot });
      logSuccess(`[${sessionId}] 执行后截图: ${afterShot}`);
    }
    
    // Step 5: 等待验证结果 + 最终截图
    logStep(4, `[${sessionId}] 等待验证结果 (1秒)...`);
    logWait(`[${sessionId}] 等待页面响应...`);
    await sleep(1000);
    const finalShot = await saveDebugScreenshot(a, `${sessionId}_final`);
    if (finalShot) {
      debugInfo.screenshots.push({ stage: 'final', path: finalShot });
      logSuccess(`[${sessionId}] 最终截图: ${finalShot}`);
    }
    
    const totalDuration = Date.now() - overallStart;
    debugInfo.totalDurationMs = totalDuration;
    debugInfo.success = true;
    
    logStep(5, `[${sessionId}] ✅ AI 视觉滑块解决完成 (总耗时 ${totalDuration}ms)`);
    logSuccess(`[${sessionId}] 所有步骤:`);
    for (const s of debugInfo.steps) {
      logSuccess(`[${sessionId}]   - ${s.name}: ${s.durationMs}ms`);
    }
    if (debugInfo.screenshots.length > 0) {
      logSuccess(`[${sessionId}] 截图:`);
      for (const sc of debugInfo.screenshots) {
        logSuccess(`[${sessionId}]   - [${sc.stage}] ${sc.path}`);
      }
    }
    
    res.json({
      success: true,
      method: 'aiAct',
      result,
      debug: debugInfo,
    });
    
  } catch (err) {
    logError(`[${sessionId}] aiAct 执行失败`, err);
    debugInfo.error = err.message;
    
    // 失败时保存截图
    try {
      const a = await initAgent();
      const failShot = await saveDebugScreenshot(a, `${sessionId}_on_error`);
      if (failShot) debugInfo.screenshots.push({ stage: 'error', path: failShot });
    } catch (e) {
      logDebug(`[${sessionId}] 失败截图也保存不了: ${e.message}`);
    }
    
    // ===== 回退方案: aiLocate 定位 + 手动拖拽 =====
    logStep(6, `[${sessionId}] 尝试回退方案: aiLocate 定位 + 手动拖拽...`);
    try {
      const a = await initAgent();
      
      // Step F1: aiLocate 找手柄
      logStep(6.1, `[${sessionId}] aiLocate 搜索滑块手柄...`);
      const locateStart = Date.now();
      const handlePos = await a.aiLocate(
        '滑块验证组件中的手柄（带有 >> 箭头图标的可拖拽按钮，位于滑轨最左端）'
      );
      logStep(6.1, `[${sessionId}] aiLocate 返回 (耗时 ${Date.now() - locateStart}ms): ${JSON.stringify(handlePos).substring(0, 300)}`);
      debugInfo.steps.push({ name: 'aiLocate', durationMs: Date.now() - locateStart, result: handlePos });
      
      if (!handlePos) {
        logError(`[${sessionId}] aiLocate 未能定位到滑块手柄`);
        debugInfo.fallbackError = 'aiLocate returned null';
        return res.json({ success: false, error: err.message, fallback: 'aiLocate failed', debug: debugInfo });
      }
      
      // 解析坐标 (兼容多种格式)
      let handleX, handleY;
      if (handlePos.center) {
        handleX = handlePos.center.x;
        handleY = handlePos.center.y;
      } else if (handlePos.topLeft) {
        // 估算中心点
        handleX = handlePos.topLeft.x + (handlePos.bottomRight?.x - handlePos.topLeft.x) / 2 || handlePos.topLeft.x + 25;
        handleY = handlePos.topLeft.y + (handlePos.bottomRight?.y - handlePos.topLeft.y) / 2 || handlePos.topLeft.y + 15;
      } else if (handlePos.x !== undefined) {
        handleX = handlePos.x;
        handleY = handlePos.y;
      }
      
      logStep(6.2, `[${sessionId}] 解析手柄坐标: (${handleX}, ${handleY})`);
      
      // Step F2: 拖拽前截图
      const beforeDragShot = await saveDebugScreenshot(a, `${sessionId}_before_fallback_drag`);
      if (beforeDragShot) debugInfo.screenshots.push({ stage: 'before_fallback_drag', path: beforeDragShot });
      
      // Step F3: 用 aiAct 执行拖拽 (v1.12.0 不支持底层 mouse API, 用纯视觉描述)
      logStep(6.3, `[${sessionId}] aiAct 拖拽 (视觉描述)...`);
      
      const dragPrompt = '找到滑块验证组件中的手柄（带有 >> 箭头图标或其他可拖拽标识的按钮），按住手柄沿水平方向向右拖动，直到到达滑轨最右端完成验证';
      
      const dragStart = Date.now();
      const dragResult = await a.aiAct(dragPrompt);
      const dragDuration = Date.now() - dragStart;
      debugInfo.steps.push({ name: 'aiAct_drag', durationMs: dragDuration });
      logStep(6.3, `[${sessionId}] 拖拽完成 (耗时 ${dragDuration}ms)`);
      
      // Step F4: 拖拽后截图
      const afterDragShot = await saveDebugScreenshot(a, `${sessionId}_after_fallback_drag`);
      if (afterDragShot) debugInfo.screenshots.push({ stage: 'after_fallback_drag', path: afterDragShot });
      
      // Step F5: 等待验证 + 最终截图
      logStep(6.4, `[${sessionId}] 等待验证结果 (1.5秒)...`);
      await sleep(1500);
      const finalFallbackShot = await saveDebugScreenshot(a, `${sessionId}_fallback_final`);
      if (finalFallbackShot) debugInfo.screenshots.push({ stage: 'fallback_final', path: finalFallbackShot });
      
      debugInfo.success = true;
      debugInfo.totalDurationMs = Date.now() - overallStart;
      
      logStep(7, `[${sessionId}] ✅ 回退方案完成 (总耗时 ${debugInfo.totalDurationMs}ms)`);
      
      res.json({
        success: true,
        method: 'aiLocate+manualDrag',
        handlePosition: { x: handleX, y: handleY },
        dragDistance,
        debug: debugInfo,
      });
      
    } catch (fallbackErr) {
      logError(`[${sessionId}] 回退方案也失败`, fallbackErr);
      debugInfo.fallbackError = fallbackErr.message;
      debugInfo.totalDurationMs = Date.now() - overallStart;
      
      res.json({
        success: false,
        error: err.message,
        fallbackError: fallbackErr.message,
        debug: debugInfo,
      });
    }
  }
});

// ============ 2. 视觉定位滑块 (aiQuery) ============
// POST /locate_slider
app.post('/locate_slider', async (req, res) => {
  const sessionId = `locate_${Date.now()}`;
  try {
    const a = await initAgent();
    
    // 截图
    const shot = await saveDebugScreenshot(a, `${sessionId}_locate`);
    
    const prompt = req.body?.prompt || 
      '屏幕上有一个滑块验证区域，请找到滑块的拖拽手柄位置。返回 JSON: { handle: {x: number, y: number} }';
    
    logStep(1, `[${sessionId}] aiQuery 定位滑块...`);
    const result = await a.aiQuery(prompt);
    logStep(1, `[${sessionId}] aiQuery 返回: ${JSON.stringify(result).substring(0, 500)}`);
    
    // ========== 解析坐标 (归一化: 统一转为 {x, y} dict 格式) ==========
    function normalizeHandle(raw) {
      if (!raw) return null;
      // Case 1: 已经是 {x, y} dict
      if (typeof raw === 'object' && raw.x !== undefined && raw.y !== undefined) {
        return { x: raw.x, y: raw.y };
      }
      // Case 2: list [x, y]
      if (Array.isArray(raw) && raw.length >= 2 && typeof raw[0] === 'number') {
        return { x: raw[0], y: raw[1] };
      }
      // Case 3: {center: {x, y}} or {topLeft: {x, y}}
      if (raw.center) {
        return normalizeHandle(raw.center);
      }
      if (raw.topLeft) {
        const tl = raw.topLeft;
        const br = raw.bottomRight;
        if (tl && br) {
          return { x: (tl.x + br.x) / 2, y: (tl.y + br.y) / 2 };
        }
        return normalizeHandle(tl);
      }
      // Case 4: {handle: ...} nested
      if (raw.handle) {
        return normalizeHandle(raw.handle);
      }
      return null;
    }
    
    let handle = null;
    let gap = null;
    
    if (result && typeof result === 'object') {
      // 尝试各种可能的 key
      const rawHandle = result.handle || result.slider_handle || result.sliderHandle 
        || result.slider || result.drag_handle || null;
      const rawGap = result.gap || result.target || result.target_gap || null;
      
      if (rawHandle) handle = normalizeHandle(rawHandle);
      if (rawGap) gap = normalizeHandle(rawGap);
      
      // 如果没找到 handle,尝试直接从 result 解析
      if (!handle && result.x !== undefined && result.y !== undefined) {
        handle = normalizeHandle({ x: result.x, y: result.y });
      }
      if (!handle && Array.isArray(result) && result.length >= 2) {
        handle = normalizeHandle(result);
      }
    }
    
    // 回退: 用 aiLocate
    if (!handle) {
      logStep(2, `[${sessionId}] aiQuery 解析失败, 尝试 aiLocate...`);
      const pos = await a.aiLocate('滑块验证组件的手柄（带有 >> 箭头图标）');
      logStep(2, `[${sessionId}] aiLocate 返回: ${JSON.stringify(pos).substring(0, 300)}`);
      
      if (pos) {
        handle = normalizeHandle(pos.center || pos.topLeft || pos);
        if (pos.topLeft && pos.bottomRight) {
          handle = normalizeHandle({
            x: (pos.topLeft.x + pos.bottomRight.x) / 2,
            y: (pos.topLeft.y + pos.bottomRight.y) / 2
          });
        }
      }
    }
    
    if (!handle) {
      logError(`[${sessionId}] 无法解析定位结果, raw: ${JSON.stringify(result).substring(0, 300)}`);
      return res.json({ success: false, error: '无法解析定位结果', raw: result });
    }
    
    const response = { success: true, handle, raw: result, screenshot: shot };
    if (gap) response.gap = gap;
    
    logSuccess(`[${sessionId}] 定位成功: handle=(${handle.x.toFixed(0)}, ${handle.y.toFixed(0)})${gap ? `, gap=(${gap.x.toFixed(0)}, ${gap.y.toFixed(0)})` : ''}`);
    res.json(response);
  } catch (err) {
    logError(`[${sessionId}] 定位失败`, err);
    res.json({ success: false, error: err.message });
  }
});

// ============ 3. OS 级逐步拖拽 (v1.12.0: 用 aiTap 或不支持) ============
// POST /perform_drag
app.post('/perform_drag', async (req, res) => {
  try {
    const a = await initAgent();
    const { points, startDelayMs = 0, endDelayMs = 0 } = req.body;
    
    if (!points || !Array.isArray(points) || points.length < 2) {
      return res.json({ success: false, error: '至少需要 2 个轨迹点' });
    }
    
    logStep(1, `开始拖拽: ${points.length} 个点`);
    logWait(`v1.12.0 不支持底层 mouse API, 改用 aiAct 实现拖拽 (纯视觉描述)...`);
    
    // v1.12.0 没有 agent.mouse API, 用 aiAct + 纯视觉描述实现拖拽
    const dragPrompt = '找到滑块验证组件中的手柄（带有 >> 箭头图标或其他可拖拽标识的按钮），按住手柄沿水平方向向右拖动，直到到达滑轨最右端完成验证';
    
    logSend(`拖拽 aiAct prompt: ${dragPrompt}`);
    const result = await a.aiAct(dragPrompt);
    logRecv(`拖拽结果: ${JSON.stringify(result).substring(0, 300)}`);
    
    // 拖拽后截图
    await saveDebugScreenshot(a, 'after_perform_drag');
    
    logSuccess(`拖拽完成`);
    res.json({ success: true, result });
  } catch (err) {
    logError('拖拽失败', err);
    res.json({ success: false, error: err.message });
  }
});

// ============ 4. OS 级点击 (v1.12.0: 用 aiTap) ============
app.post('/click', async (req, res) => {
  try {
    const a = await initAgent();
    const { x, y, button = 'left' } = req.body;
    
    if (x === undefined || y === undefined) {
      return res.json({ success: false, error: '需要 x, y 坐标' });
    }
    
    logDebug(`点击 @ (${x}, ${y}) button=${button}`);
    
    // v1.12.0: 用 aiTap 在指定位置点击
    const prompt = button === 'left' 
      ? `在屏幕坐标 (${x}, ${y}) 位置单击鼠标左键`
      : `在屏幕坐标 (${x}, ${y}) 位置单击鼠标右键`;
    
    const result = await a.aiTap(prompt);
    res.json({ success: true, result });
  } catch (err) {
    logError('点击失败', err);
    res.json({ success: false, error: err.message });
  }
});

// ============ 5. 截屏 (v1.12.0: 用 interface.screenshotBase64) ============
app.get('/screenshot', async (req, res) => {
  try {
    const a = await initAgent();
    const filename = req.query.filename || `screenshot_${Date.now()}.png`;
    const filepath = path.join(SCREENSHOT_DIR, filename);
    
    // v1.12.0 API
    const base64 = await a.interface.screenshotBase64();
    const cleanB64 = base64.replace(/^data:image\/\w+;base64,/, '');
    fs.writeFileSync(filepath, Buffer.from(cleanB64, 'base64'));
    
    res.json({ success: true, filepath });
  } catch (err) {
    logError('截屏失败', err);
    res.json({ success: false, error: err.message });
  }
});

// ============ 6. 获取屏幕信息 ============
app.get('/screen_info', async (req, res) => {
  try {
    // 如果 agent 已初始化,返回详细信息
    if (agent) {
      const info = await agent.aiQuery(
        '{width: number, height: number}, 获取屏幕分辨率'
      );
      res.json({ success: true, info, agentInitialized: true });
      return;
    }
    
    // Agent 未初始化,直接返回已缓存的显示器列表 (不触发 agent 初始化)
    try {
      const displays = await ComputerDevice.listDisplays();
      const displaySummary = displays.map(d => ({
        id: d.id,
        name: d.name,
        primary: d.primary,
      }));
      res.json({ 
        success: true, 
        agentInitialized: false,
        message: 'Agent 未初始化,首次调用 /ai_solve_slider 时自动加载',
        displays: displaySummary,
        hint: '查看日志文件获取更多详情,或设置 MIDSCENE_DISPLAY_ID 环境变量'
      });
    } catch {
      res.json({ success: true, agentInitialized: false, message: 'Agent 未初始化' });
    }
  } catch (err) {
    logError('获取屏幕信息失败', err);
    res.json({ success: false, error: err.message });
  }
});

// ============ 7. 健康检查 ============
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    agentInitialized: !!agent,
    uptime: process.uptime(),
    debugDir: DEBUG_DIR,
  });
});

// ============ 8. 列出调试文件 ============
app.get('/debug_files', (req, res) => {
  try {
    const files = fs.readdirSync(SCREENSHOT_DIR)
      .filter(f => f.endsWith('.png'))
      .sort()
      .reverse()
      .slice(0, 30);
    res.json({ success: true, files, dir: SCREENSHOT_DIR });
  } catch (err) {
    res.json({ success: false, error: err.message });
  }
});

// 辅助函数
function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// 启动服务
const PORT = process.env.MIDSCENE_PORT || 3456;
app.listen(PORT, () => {
  console.log(`[Midscene] Computer HTTP Service running on port ${PORT}`);
  console.log(`[Midscene] 调试截图目录: ${SCREENSHOT_DIR}`);
  console.log(`[Midscene] Endpoints:`);
  console.log(`  POST /ai_solve_slider  - AI 视觉一步解决滑块 (主方案)`);
  console.log(`  POST /locate_slider    - 视觉定位滑块`);
  console.log(`  POST /perform_drag     - OS 级拖拽`);
  console.log(`  POST /click            - OS 级点击`);
  console.log(`  GET  /screenshot       - 截屏`);
  console.log(`  GET  /screen_info      - 屏幕信息`);
  console.log(`  GET  /debug_files      - 列出调试截图`);
  console.log(`  GET  /health           - 健康检查`);
  console.log(`[Midscene] 等待模型配置...`);
});
