/** 后台生成任务的步骤名由后端固定（app/generator.py 的 STEPS），这里只负责显示文案。进度页和「正在生成」卡片共用。 */
export const STEP_LABEL: Record<string, { title: string; hint: string }> = {
  library: { title: "读取地点库", hint: "加载主题和已核对的地点、闭馆日、官网链接" },
  schedule: { title: "选点并排程", hint: "按主题选点，避开闭馆日，分天、排路线、算时间" },
  validate: { title: "质检", hint: "逐条跑质检规则" },
  narrate: { title: "生成说明", hint: "让模型写每天的主题和点评；模型不可用时用模板" },
};
