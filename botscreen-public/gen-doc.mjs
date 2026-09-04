import { Document, Packer, Paragraph, TextRun, Table, TableCell, TableRow, WidthType, ShadingType, convertInchesToTwip } from 'docx';
import fs from 'fs';

function cell(text, isHeader = false) {
  return new TableCell({
    children: [new Paragraph({
      children: [new TextRun({ text, bold: isHeader, size: 20, color: isHeader ? 'FFFFFF' : '1e293b' })]
    })],
    shading: isHeader ? { type: ShadingType.SOLID, color: '168378' } : undefined,
  });
}

const h1 = (text) => new Paragraph({ spacing: { before: 500, after: 240 }, children: [new TextRun({ text, size: 36, bold: true, color: '168378' })] });
const h2 = (text) => new Paragraph({ spacing: { before: 300, after: 120 }, children: [new TextRun({ text, size: 28, bold: true, color: '0f172a' })] });
const h3 = (text) => new Paragraph({ spacing: { before: 240, after: 100 }, children: [new TextRun({ text, size: 24, bold: true })] });
const p = (text, opts = {}) => new Paragraph({ spacing: { after: 100 }, children: [new TextRun({ text, size: 21, ...opts })] });
const pb = (label, text) => new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: label, bold: true, size: 21 }), new TextRun({ text, size: 21 })] });

const makeTable = (headers, rows) => new Table({
  rows: [
    new TableRow({ children: headers.map(h => cell(h, true)) }),
    ...rows.map(r => new TableRow({ children: r.map(c => cell(c)) }))
  ],
  width: { size: 100, type: WidthType.PERCENTAGE },
});

const doc = new Document({
  styles: { default: { document: { run: { font: 'Microsoft YaHei', size: 21 } } } },
  sections: [
    // 封面
    {
      properties: { page: { margin: { top: convertInchesToTwip(1.2), bottom: convertInchesToTwip(1), left: convertInchesToTwip(1.2), right: convertInchesToTwip(1.2) } } },
      children: [
        new Paragraph({ spacing: { before: 2400 }, children: [new TextRun({ text: 'botscreen 互动问答功能', size: 56, bold: true, color: '168378' })] }),
        new Paragraph({ spacing: { before: 200 }, children: [new TextRun({ text: '语音交互 · 本地知识库 · LLM 兜底 · 点阵表情', size: 28, color: '5eead4' })] }),
        new Paragraph({ spacing: { before: 800 }, children: [new TextRun({ text: '天津市眼科医院视光中心 · 机器人屏幕交互方案', size: 22, color: '64748b' })] }),
        new Paragraph({ spacing: { before: 1200 }, children: [new TextRun({ text: '2026 年 7 月', size: 20, color: '94a3b8' })] }),
      ]
    },
    // 正文
    {
      properties: { page: { margin: { top: convertInchesToTwip(1), bottom: convertInchesToTwip(1), left: convertInchesToTwip(1.2), right: convertInchesToTwip(1.2) } } },
      children: [
        h1('一、概述'),
        p('本功能为 botscreen 信息展示系统的第四个核心模块。用户在机器人屏幕（横屏平板）上通过语音提问或点击示例问题，系统先从本地知识库匹配答案，未命中时调用 DeepSeek 大模型兜底回答。全程配合 16×16 点阵 LED 表情（小视），通过微笑、聆听、开心等表情反馈交互状态。'),
        p('核心原则：语音优先、单次问答、用完即走。不做聊天历史和打字输入，适合医院公共设备的场景特点。'),

        h1('二、用户交互流程'),

        h2('阶段一：待机欢迎'),
        pb('左侧：', '😊 微笑点阵表情 + 问候语"你好，我是你的AI助手 小视，有什么可以帮助你的？" + [🎤 点击说话] 按钮。'),
        pb('右侧：', '6 个示例问题卡片（2×3 网格），如"角膜塑形镜适合多大年龄？"、"散瞳有副作用吗？"、"什么是假性近视？"等。点击任意示例直接跳到阶段三。'),
        pb('超时：', '30 秒无操作，表情切换为 😴 休眠。用户再次走近或点击屏幕唤醒。'),

        h2('阶段二：正在聆听'),
        pb('触发：', '用户点击麦克风按钮，或语音采集程序检测到人声输入。'),
        pb('左侧：', '🎤 聆听表情（O嘴 + 睁大眼），问候语变为"正在聆听..."，光晕脉冲动画加速。'),
        pb('右侧：', '7 根跳动声波柱 + 扩散圆环动画 + "请说出您的问题" + [取消] 按钮。'),
        pb('超时：', '5 秒未检测到有效语音 → 自动回到阶段一。识别到文字 → 自动进入阶段三。'),

        h2('阶段三：展示答案'),
        pb('左侧：', '😄 开心表情，问候语变为"好的，这是你要的答案"。'),
        pb('右侧：', '答案卡片，含问题回顾 + Markdown 正文 + 来源标注 + [🔄 继续提问] + [🔊 语音播报]。'),
        pb('来源标注：', '本地知识库命中 → 绿色 ✅ 来自知识库 · 已审核。LLM 生成 → 黄色 ⚠️ AI生成 · 仅供参考 · 请咨询专业医生。'),
        pb('超时：', '30 秒无操作 → 自动回到阶段一。'),

        h1('三、技术架构'),

        h3('3.1 硬件层'),
        p('M260C 六麦克风阵列通过 USB 连接机器人主机（Linux），ROS 节点处理声源定位与原始音频采集，输出给讯飞 ASR SDK 完成语音转文字。'),

        h3('3.2 软件分层'),
        makeTable(
          ['层级', '进程', '职责'],
          [
            ['语音采集服务', '独立进程 (Python/C++)', 'M260C 音频 → 讯飞 ASR → 文字。通过 localhost WebSocket 发送给 Electron 主进程。消息格式：{ text, angle }'],
            ['Electron 主进程', 'Node.js (main)', 'voice-server.ts 接收语音文字 → qa-matcher.ts 本地关键词匹配 → 命中返回 Markdown 路径，未命中调用 DeepSeek API 流式返回。通过 IPC push 推送给渲染进程。'],
            ['Electron 渲染进程', 'Chromium (renderer)', 'qa/index.vue 三阶段 UI 切换 + FaceCanvas.vue 点阵表情 + VoiceStatus.vue 声波动画。纯展示层，不做数据计算。'],
          ]
        ),

        h3('3.3 与现有架构的关系'),
        p('全部增量添加，不破坏任何现有代码。四个功能页面统一复用同一套架构：YAML 配置驱动 → API 读取 → 组件渲染。'),
        makeTable(
          ['现有（不动）', '新增'],
          [
            ['src/main/index.ts (新增注册)', 'src/main/voice-server.ts'],
            ['src/main/api.ts', 'src/main/qa-matcher.ts'],
            ['', 'src/main/llm-client.ts'],
            ['src/preload/index.ts (新增 API)', 'src/renderer/src/pages/qa/index.vue'],
            ['src/renderer/src/pages/science/', 'src/renderer/src/components/FaceCanvas.vue'],
            ['src/renderer/src/pages/member/', 'src/renderer/src/components/VoiceStatus.vue'],
            ['src/renderer/src/pages/about/', 'RCPATH/qa/knowledge.yml + *.md'],
          ]
        ),

        h1('四、数据文件设计'),
        p('所有数据文件放在 RCPATH/qa/ 目录下（生产环境 /opt/botscreen/config/qa/，开发环境 D:\\config\\qa\\）。'),
        p('目录结构：', { font: 'Consolas' }),
        p('qa/\n├── knowledge.yml    ← 本地知识库（核心）\n├── 角膜塑形镜.md\n├── 散瞳副作用.md\n├── 假性近视.md\n└── ...', { font: 'Consolas' }),
        h3('knowledge.yml 字段说明：'),
        makeTable(
          ['字段', '类型', '说明'],
          [
            ['question', 'string', '完整问题文本，用于示例展示和管理端查看'],
            ['keywords', 'string[]', '关键词列表，用户口语匹配用。如 [角膜塑形镜, OK镜, 年龄, 几岁]'],
            ['answer', 'string', '答案 Markdown 文件路径，如 qa/角膜塑形镜.md'],
            ['tags', 'string[]', '分类标签，预留筛选功能。如 [角膜塑形镜, 儿童]'],
          ]
        ),

        h1('五、本地匹配逻辑'),
        p('采用关键词交集打分策略（先简单后复杂）：'),
        p('1. 用户输入转小写 + 去标点'),
        p('2. 遍历 knowledge.yml 每条知识，计算用户文本与 keywords 的交集命中率（命中数 / 总关键词数）'),
        p('3. 最高分 > 阈值（初始 30%）→ 命中，返回对应 .md 文件路径'),
        p('4. 所有条目均低于阈值 → 未命中 → 调用 DeepSeek API'),
        p('后续可升级为 embedding 向量相似度检索。关键词方案在知识库较小时足够且无需额外基础设施。', { italics: true }),

        h1('六、大模型接入方案'),
        pb('模型选择：', 'DeepSeek（中文能力强、价格低、支持流式 SSE、可私有化部署）。'),
        pb('调用方式：', '主进程 llm-client.ts 通过 fetch 调用 DeepSeek API，stream: true，逐 chunk 通过 IPC 推送到渲染进程。'),
        pb('系统提示词：', '"你是天津市眼科医院视光中心的科普助手。只回答眼科、视光、眼健康相关问题。非眼科问题礼貌拒绝。使用通俗中文，不确定时明确建议到院咨询。"'),
        pb('安全性：', 'API Key 放在主进程环境变量，渲染进程永远不接触。'),

        h3('回答来源标注规则：'),
        makeTable(
          ['来源', '标注', '颜色'],
          [
            ['本地知识库命中', '✅ 来自知识库 · 已审核', '绿色 #5eead4'],
            ['LLM 生成', '⚠️ AI生成 · 仅供参考 · 请咨询专业医生', '黄色 #f59e0b'],
            ['审核入库后', '追加到 knowledge.yml + 标注审核日期，下次走本地匹配', '绿色'],
          ]
        ),

        h1('七、IPC 通道设计'),
        p('在现有 preload/index.ts 4 个方法基础上新增，互不影响。'),
        h3('渲染进程 → 主进程（invoke）：'),
        makeTable(
          ['通道', '方向', '说明'],
          [
            ['qa:get-examples', '渲染→主', '读取 knowledge.yml，返回前 6 条作为示例问题'],
            ['qa:ask-text', '渲染→主', '手动输入文字，走匹配→回答流程'],
            ['qa:ask-voice-start', '渲染→主', '通知主进程开始接收语音'],
            ['qa:ask-voice-stop', '渲染→主', '通知主进程停止语音接收'],
          ]
        ),
        h3('主进程 → 渲染进程（push）：'),
        makeTable(
          ['通道', '方向', '说明'],
          [
            ['qa:voice-status', '主→渲染', '{ status: listening | processing | idle }'],
            ['qa:answer-ready', '主→渲染', '{ question, content, source } —— 本地命中一次性返回'],
            ['qa:answer-chunk', '主→渲染', '{ content } —— LLM 流式逐块推送'],
            ['qa:answer-done', '主→渲染', '{} —— 流式回答结束信号'],
          ]
        ),

        h1('八、点阵表情系统'),
        p('16×16 LED 点阵风格，用 #5eead4 圆点拼出眼睛和嘴巴。通过 Canvas 渲染，接受 expression prop 控制表情切换。'),
        makeTable(
          ['表情', '触发条件', '视觉特征'],
          [
            ['😊 微笑', '待机、欢迎、回答完毕', '眼睛弯成 ^ ^，嘴巴小弧线'],
            ['🎤 聆听', '语音唤醒、正在收音', '眼睛睁大、嘴巴 O 形，随声波节奏缩放'],
            ['🤔 思考', '查知识库、等 LLM 返回', '眼睛对眼向内看，嘴巴歪向一边'],
            ['😄 开心', '匹配成功、回答展示', '眼睛高度弯曲，嘴巴大笑弧线'],
            ['😵 困惑', '没听清、置信度低', '眼睛变成 > <，请再说一遍'],
            ['😴 休眠', '30秒无交互、夜间', '眼睛眯成 = =，呼吸灯慢速明暗'],
            ['👋 打招呼', '人脸检测、系统启动', '眼睛短暂放大再恢复（弹性动画）'],
            ['🔧 故障', '网络断开、API 异常', '眼睛变 X X，表示需要维护'],
          ]
        ),
        pb('表情组件复用：', '首页显示 160px 小尺寸微笑表情（始终微笑），/qa 页面显示 130px 表情（跟随三阶段联动切换）。同一组件不同尺寸。'),

        h1('九、边界情况处理'),
        makeTable(
          ['情况', '处理方式'],
          [
            ['语音识别为空', '回到阶段一，短暂显示 😵 困惑表情，"没听清，请再说一遍"'],
            ['5 秒没人说话', '自动取消聆听，回到阶段一'],
            ['知识库无匹配 + 无网络', '"抱歉，网络连接异常，请稍后再试"'],
            ['LLM 返回超时', '10 秒超时，回到阶段一，提示稍后再试'],
            ['用户快速连续点击', '防抖，新问题到达时取消上一次未完成的请求'],
            ['语音采集程序崩溃', 'WebSocket 断连自动重连，Electron 主应用不受影响'],
            ['API Key 泄露', 'Key 仅存主进程环境变量，渲染进程永远接触不到'],
          ]
        ),

        h1('十、分期实现计划'),

        h2('第一期：本地知识库 + 手动点击（预计 1-2 天）'),
        p('□ knowledge.yml + .md 预设答案准备'),
        p('□ qa-matcher.ts 本地关键词匹配'),
        p('□ qa/index.vue 横屏三阶段页面'),
        p('□ FaceCanvas.vue 点阵表情组件'),
        p('□ preload 新增 IPC 通道'),
        p('□ main/index.ts 注册新 IPC handlers'),
        p('□ 示例问题点击 → 出答案，验证完整链路'),

        h2('第二期：大模型兜底（预计半天）'),
        p('□ llm-client.ts DeepSeek API 调用 + 流式转发'),
        p('□ 流式回答渲染（逐字显示）'),
        p('□ 来源标注区分（本地/LLM）'),

        h2('第三期：语音接入（预计 1-2 天，依赖硬件到位）'),
        p('□ voice-server.ts WebSocket 服务端'),
        p('□ VoiceStatus.vue 声波动画组件'),
        p('□ 语音采集程序（独立进程，Python/C++）联调'),
        p('□ 端到端：说话 → 识别 → 匹配 → 展示答案'),

        h2('第四期：体验打磨（预计 1 天）'),
        p('□ 眨眼动画（待机时每 3 秒眨眼一次）'),
        p('□ 休眠/唤醒（30 秒无操作 → 😴）'),
        p('□ TTS 语音播报答案'),
        p('□ LLM 回答审核入库流程'),

        h1('十一、新增文件清单'),
        makeTable(
          ['文件', '职责', '预计行数'],
          [
            ['src/main/voice-server.ts', 'WebSocket 服务端，接收语音采集程序文字', '~80'],
            ['src/main/qa-matcher.ts', '本地知识库关键词匹配', '~50'],
            ['src/main/llm-client.ts', 'DeepSeek API 调用 + 流式 SSE 转发', '~60'],
            ['src/renderer/src/pages/qa/index.vue', '问答页面，三阶段切换 + 横屏布局', '~200'],
            ['src/renderer/src/components/FaceCanvas.vue', '16×16 点阵表情组件', '~100'],
            ['src/renderer/src/components/VoiceStatus.vue', '声波动画 + 圆环扩散', '~50'],
            ['RCPATH/qa/knowledge.yml', '本地知识库数据文件', '按需'],
            ['RCPATH/qa/*.md', '各预设问题的答案 Markdown', '按需'],
          ]
        ),

        h3('需修改的文件：'),
        p('src/main/index.ts — app.whenReady() 里初始化 voice-server + 注册新增 IPC handlers'),
        p('src/preload/index.ts — 新增 voiceEvents 监听 + qaApi 方法'),
        p('src/preload/index.d.ts — 新增 TypeScript 类型声明'),
      ]
    }
  ]
});

const buffer = await Packer.toBuffer(doc);
fs.writeFileSync('D:/botscreen初版计划/01.docx', buffer);
console.log('Done! File written successfully.');
