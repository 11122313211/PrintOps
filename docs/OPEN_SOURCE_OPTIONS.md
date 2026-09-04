# 可借用的开源组件

当前环境无法稳定访问 GitHub，因此这里先按项目公开定位做选型，不复制任何未经核对的代码。MVP 采用标准库实现同等边界，安装依赖后可逐项替换。

| 项目 | GitHub | 适合借用的能力 | 接入时机 |
| --- | --- | --- | --- |
| PydanticAI | [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | Agent、工具函数、结构化输出、依赖注入 | 接入真实模型时优先 |
| LangGraph | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | 有状态工作流、检查点、人工介入节点 | 订单流程超过 3 个阶段后 |
| LiteLLM | [BerriAI/litellm](https://github.com/BerriAI/litellm) | 统一 OpenAI/国产/本地模型接口 | 需要切换模型供应商时 |
| Qdrant | [qdrant/qdrant](https://github.com/qdrant/qdrant) | 工艺知识库向量检索与过滤 | 工艺资料超过几十篇时 |
| PyMuPDF | [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF) | PDF 页尺寸、图片、字体和元数据检查 | 加入正式印前预检时 |

借用原则：模型只做理解、规划和工具选择；订单字段校验、工艺兼容性、文件预检和最终提交确认由确定性代码完成。这样换模型或换印刷平台时，订单协议不会漂移。
