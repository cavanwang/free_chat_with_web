#!/usr/bin/env node
/**
 * Midscene API 连通性检测
 * 在启动服务前验证:
 *   1. 必要的环境变量是否配置
 *   2. API Key 是否有效
 *   3. 是否能成功调用 AI 模型
 * 
 * 用法: node test-api.js
 * 返回: 0 = 成功, 1 = 失败
 */

const { agentFromComputer } = require('@midscene/computer');

// ============ 1. 检查必要环境变量 ============
console.log('🔍 检查环境变量...');

const required = [
  'MIDSCENE_MODEL_API_KEY',
  'MIDSCENE_MODEL_NAME',
  'MIDSCENE_MODEL_FAMILY',
  'MIDSCENE_MODEL_BASE_URL'
];

let missing = [];
for (const key of required) {
  if (!process.env[key]) {
    missing.push(key);
  }
}

if (missing.length > 0) {
  console.error(`❌ 缺少必要的环境变量: ${missing.join(', ')}`);
  console.error('   请设置:');
  console.error('   export MIDSCENE_MODEL_API_KEY="your-key"');
  console.error('   export MIDSCENE_MODEL_NAME="qwen3.7-plus"');
  console.error('   export MIDSCENE_MODEL_FAMILY="qwen3"');
  console.error('   export MIDSCENE_MODEL_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"');
  process.exit(1);
}

console.log('✅ 所有环境变量已配置');
console.log(`   模型: ${process.env.MIDSCENE_MODEL_NAME} (${process.env.MIDSCENE_MODEL_FAMILY})`);
console.log(`   API: ${process.env.MIDSCENE_MODEL_BASE_URL}`);
console.log(`   Key : ${process.env.MIDSCENE_MODEL_API_KEY.substring(0, 8)}...`);

// ============ 2. 测试 API 连通性 ============
console.log('\n🔌 测试 API 连通性...');

(async () => {
  let agent = null;
  try {
    const startTime = Date.now();
    
    agent = await agentFromComputer({
      aiActionContext: '连通性测试',
    });
    
    // 执行一个简单的查询来验证模型可用
    const result = await agent.aiQuery(
      '{status: string}, 只返回 "ok" 两个字'
    );
    
    const elapsed = Date.now() - startTime;
    
    if (result && typeof result === 'object') {
      console.log(`✅ API 连通成功! (耗时 ${elapsed}ms)`);
      console.log(`   模型响应: ${JSON.stringify(result)}`);
      process.exit(0);
    } else {
      console.error(`❌ API 响应异常: ${JSON.stringify(result)}`);
      process.exit(1);
    }
  } catch (err) {
    console.error(`❌ API 调用失败: ${err.message}`);
    
    // 分析常见错误
    if (err.message.includes('401') || err.message.includes('Unauthorized')) {
      console.error('   → API Key 无效,请检查 MIDSCENE_MODEL_API_KEY');
    } else if (err.message.includes('403') || err.message.includes('Forbidden')) {
      console.error('   → 无访问权限,请检查 API Key 和账户余额');
    } else if (err.message.includes('404') || err.message.includes('Not Found')) {
      console.error('   → 模型不存在,请检查 MIDSCENE_MODEL_NAME 是否正确');
    } else if (err.message.includes('timeout') || err.message.includes('ETIMEDOUT')) {
      console.error('   → 网络超时,请检查网络连接和 MIDSCENE_MODEL_BASE_URL');
    } else if (err.message.includes('ECONNREFUSED')) {
      console.error('   → 无法连接到 API 服务,请检查 MIDSCENE_MODEL_BASE_URL');
    } else if (err.message.includes('quota') || err.message.includes('insufficient') || err.message.includes('balance')) {
      console.error('   → 账户余额不足或配额已用完');
    } else {
      console.error('   → 未知错误,请检查配置');
    }
    
    console.error('\n   当前配置:');
    console.error(`     MIDSCENE_MODEL_API_KEY=${process.env.MIDSCENE_MODEL_API_KEY.substring(0, 8)}...`);
    console.error(`     MIDSCENE_MODEL_NAME=${process.env.MIDSCENE_MODEL_NAME}`);
    console.error(`     MIDSCENE_MODEL_FAMILY=${process.env.MIDSCENE_MODEL_FAMILY}`);
    console.error(`     MIDSCENE_MODEL_BASE_URL=${process.env.MIDSCENE_MODEL_BASE_URL}`);
    
    process.exit(1);
  }
})();