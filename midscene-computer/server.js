/**
 * Midscene Computer HTTP Service
 * 路线 A: OS 级视觉定位 + OS 级键鼠操作
 * 
 * 功能:
 *   - 纯视觉定位滑块(不依赖 DOM)
 *   - OS 级逐步执行拖拽(支持物理仿真轨迹)
 *   - 截屏用于调试
 * 
 * Python 端通过 HTTP 调用此服务
 */

const express = require('express');
const { agentFromComputer } = require('@midscene/computer');

const app = express();
app.use(express.json({ limit: '50mb' }));

// 全局 agent(复用连接,避免每次请求都重新初始化)
let agent = null;
let initPromise = null;

async function initAgent() {
  if (agent) return agent;
  if (initPromise) return initPromise;
  
  initPromise = (async () => {
    console.log('[Midscene] 初始化 Computer agent...');
    agent = await agentFromComputer({
      aiActionContext: '你正在控制一台桌面计算机，用于自动化滑块验证。',
    });
    console.log('[Midscene] Computer agent 初始化成功');
    return agent;
  })();
  
  return initPromise;
}

// ============ 1. 视觉定位滑块 ============
// POST /locate_slider
// Body: { prompt?: string }  可选的自定义描述
// Response: { success: true, handle: {x, y}, gap: {x, y}, confidence: number }
app.post('/locate_slider', async (req, res) => {
  try {
    const a = await initAgent();
    const prompt = req.body?.prompt || 
      '屏幕上有一个滑块验证区域，请找到滑块的拖拽手柄位置和目标缺口位置。返回 JSON: { handle: {x: number, y: number}, gap: {x: number, y: number} }';
    
    console.log(`[Midscene] 视觉定位滑块... prompt="${prompt}"`);
    
    const result = await a.aiQuery(prompt);
    console.log(`[Midscene] 定位结果:`, result);
    
    // 解析 AI 返回的坐标
    // Midscene aiQuery 通常返回结构化 JSON,但格式可能不一致
    // 兼容多种返回格式
    let handle = null;
    let gap = null;
    
    if (result && typeof result === 'object') {
      // 直接返回 { handle, gap }
      if (result.handle && result.gap) {
        handle = result.handle;
        gap = result.gap;
      }
      // 返回 { slider_handle, slider_gap }
      else if (result.slider_handle && result.slider_gap) {
        handle = result.slider_handle;
        gap = result.slider_gap;
      }
      // 返回 { x, y }(单点,视为 handle)
      else if (result.x !== undefined && result.y !== undefined) {
        handle = { x: result.x, y: result.y };
      }
    }
    
    if (!handle) {
      return res.json({ 
        success: false, 
        error: '无法解析定位结果',
        raw: result 
      });
    }
    
    res.json({
      success: true,
      handle,
      gap,
      confidence: 0.9,
      raw: result
    });
  } catch (err) {
    console.error('[Midscene] 定位失败:', err.message);
    res.json({ success: false, error: err.message });
  }
});

// ============ 2. OS 级逐步拖拽 ============
// POST /perform_drag
// Body: {
//   points: [{ x, y, delayMs }],  // 轨迹点序列,每个点包含坐标和到此点的延迟
//   startDelayMs?: number         // 按下鼠标前的延迟
//   endDelayMs?: number           // 松开鼠标后的延迟
// }
app.post('/perform_drag', async (req, res) => {
  try {
    const a = await initAgent();
    const { points, startDelayMs = 0, endDelayMs = 0 } = req.body;
    
    if (!points || !Array.isArray(points) || points.length < 2) {
      return res.json({ success: false, error: '至少需要 2 个轨迹点' });
    }
    
    console.log(`[Midscene] 开始拖拽: ${points.length} 个点`);
    
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    
    // 1. 移动到起点(按下前)
    await a.mouse.move(points[0].x, points[0].y);
    if (startDelayMs > 0) await sleep(startDelayMs);
    
    // 2. 按下鼠标
    await a.mouse.down();
    console.log(`[Midscene] 鼠标按下 @ (${points[0].x}, ${points[0].y})`);
    
    // 3. 逐步移动到每个轨迹点
    for (let i = 1; i < points.length; i++) {
      const p = points[i];
      const delay = p.delayMs || 10;
      await a.mouse.move(p.x, p.y);
      await sleep(delay);
    }
    
    // 4. 松开鼠标
    if (endDelayMs > 0) await sleep(endDelayMs);
    await a.mouse.up();
    
    console.log(`[Midscene] 拖拽完成`);
    res.json({ success: true });
  } catch (err) {
    console.error('[Midscene] 拖拽失败:', err.message);
    res.json({ success: false, error: err.message });
  }
});

// ============ 3. OS 级点击 ============
// POST /click
// Body: { x, y, button?: 'left'|'right'|'middle' }
app.post('/click', async (req, res) => {
  try {
    const a = await initAgent();
    const { x, y, button = 'left' } = req.body;
    
    if (x === undefined || y === undefined) {
      return res.json({ success: false, error: '需要 x, y 坐标' });
    }
    
    console.log(`[Midscene] 点击 @ (${x}, ${y}) button=${button}`);
    await a.mouse.move(x, y);
    await sleep(50); // 小停顿
    await a.mouse.click(button);
    
    res.json({ success: true });
  } catch (err) {
    console.error('[Midscene] 点击失败:', err.message);
    res.json({ success: false, error: err.message });
  }
});

// ============ 4. 截屏 ============
// GET /screenshot?filename=xxx.png
app.get('/screenshot', async (req, res) => {
  try {
    const a = await initAgent();
    const filename = req.query.filename || `screenshot_${Date.now()}.png`;
    const filepath = require('path').join(__dirname, 'screenshots', filename);
    
    require('fs').mkdirSync(require('path').dirname(filepath), { recursive: true });
    
    console.log(`[Midscene] 截屏 → ${filepath}`);
    await a.screenshot(filepath);
    
    res.json({ success: true, filepath });
  } catch (err) {
    console.error('[Midscene] 截屏失败:', err.message);
    res.json({ success: false, error: err.message });
  }
});

// ============ 5. 获取屏幕信息 ============
// GET /screen_info
app.get('/screen_info', async (req, res) => {
  try {
    const a = await initAgent();
    const info = await a.aiQuery(
      '{width: number, height: number}, 获取屏幕分辨率'
    );
    res.json({ success: true, info });
  } catch (err) {
    console.error('[Midscene] 获取屏幕信息失败:', err.message);
    res.json({ success: false, error: err.message });
  }
});

// ============ 6. 健康检查 ============
app.get('/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    agentInitialized: !!agent,
    uptime: process.uptime()
  });
});

// 辅助函数
function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// 启动服务
const PORT = process.env.MIDSCENE_PORT || 3456;
app.listen(PORT, () => {
  console.log(`[Midscene] Computer HTTP Service running on port ${PORT}`);
  console.log(`[Midscene] Endpoints:`);
  console.log(`  POST /locate_slider  - 视觉定位滑块`);
  console.log(`  POST /perform_drag   - OS 级拖拽`);
  console.log(`  POST /click          - OS 级点击`);
  console.log(`  GET  /screenshot     - 截屏`);
  console.log(`  GET  /screen_info    - 屏幕信息`);
  console.log(`  GET  /health         - 健康检查`);
  console.log(`[Midscene] 等待模型配置...`);
});