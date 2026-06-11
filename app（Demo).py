import streamlit as st
import os
import tempfile
import base64
from openai import OpenAI

# ================= 全局初始化：仅在文件不存在时创建记忆文件，避免重复写入 =================
if not os.path.exists("geo_memory.jsonl"):
    with open("geo_memory.jsonl", "w", encoding="utf-8") as f:
        f.write("")

# ================= 1. 页面基础配置 =================
st.set_page_config(
    page_title="地质AI辅助解译系统",
    page_icon="🪨",
    layout="wide"
)

# ================= 2. 侧边栏配置 =================
st.sidebar.title("⚙️ 系统设置")
# API密钥输入（密码隐藏）
api_key = st.sidebar.text_input(
    "输入 DashScope API Key",
    type="password",
    help="请在阿里云百炼平台获取"
)
# 模型选择
model_choice = st.sidebar.selectbox(
    "选择AI大模型",
    ["qwen-vl-max", "qwen-vl-plus", ]
)
# 离线模式开关
use_mock_data = st.sidebar.checkbox("🚨 启用离线模拟模式 (断网备用)")
# 版本信息
st.sidebar.info(f"当前系统版本：V1.1.0 \n架构师：虞志云")

# ================= 3. 核心AI调用函数（通义千问VL） =================
def analyze_geology_image(image_path, user_prompt, api_key, model_name):
    """
    地质图像AI解译函数
    :param image_path: 本地临时图片路径
    :param user_prompt: 地质专业提示词
    :param api_key: 阿里云API密钥
    :param model_name: 选择的大模型名称
    :return: 流式输出解译结果
    """
    # 校验API密钥是否填写
    if not api_key:
        yield "⚠️ 请在左侧边栏输入有效的 API Key！"
        return

    client = OpenAI(api_key=api_key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

    # 读取图片并转为Base64（解决云部署找不到文件问题）
    try:
        with open(image_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode('utf-8')
    except FileNotFoundError:
        yield "❌ 找不到图片文件，请检查上传！"
        return

    # 构造多模态请求消息
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                },
                {
                    "type": "text",
                    "text": user_prompt
                }
            ]
        }
    ]

    try:
        # 发起流式API请求
        completion = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.2,    # 地质解译需要严谨，降低随机性
            max_tokens=2000,     # 支持长文本报告输出
            stream=True          # 开启打字机效果
        )

        # 流式返回结果片段
        for chunk in completion:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    except Exception as e:
        yield f"❌ API调用异常: {str(e)}"

# ================= 4. 主界面布局 =================
st.title("🪨 地质AI辅助解译与成图系统")
st.markdown("---")

# 左右分栏：左侧上传图片，右侧AI解译
col1, col2 = st.columns([1, 1.2])

with col1:
    st.subheader("📥 1. 数据输入")
    # 图片上传组件
    uploaded_file = st.file_uploader("请上传野外露头或无人机照片", type=['jpg', 'png', 'jpeg'])

    # 显示已上传图片
    if uploaded_file is not None:
        st.image(uploaded_file, caption="已上传地质图像", use_column_width=True)

with col2:
    st.subheader("🤖 2. AI 智能解译")

    # 初始化会话状态，保存AI结果和临时文件路径
    if "ai_result" not in st.session_state:
        st.session_state.ai_result = ""
    if "tmp_path" not in st.session_state:
        st.session_state.tmp_path = None

    # 已上传图片时显示功能区
    if uploaded_file is not None:
        # 地质专业提示词（固定模板）
        geo_prompt = """你是一位资深的地质学专家，请对这张照片进行简单的地质解译，输出纯文本报告（不要使用任何Markdown语法）"""

        # 显示已生成的AI解译结果
        if st.session_state.ai_result:
            st.markdown("### 解译结果")
            st.write(st.session_state.ai_result)

        # 开始解译按钮
        if st.button("🚀 开始 AI 智能解译", type="primary", use_container_width=True):
            # ================= 离线模拟模式 =================
            if use_mock_data:
                st.info("🚨 当前为离线模拟模式，未调用真实 API。")
                full_response = "【离线模拟数据】中细粒黑云母二长花岗岩，浅肉红色，中细粒花岗结构，块状构造。主要矿物成分为石英、钾长石、斜长石及少量黑云母。岩石表面可见轻微风化，节理裂隙较发育，局部见铁锰质渲染。"
                st.session_state.ai_result = full_response
                st.rerun()

            # ================= 正常API调用模式 =================
            else:
                # 未输入API则终止
                if not api_key:
                    st.error("⚠️ 请先输入有效的 API Key，或勾选离线模拟模式！")
                    st.stop()

                with st.spinner("🤖 AI 正在深度解析地质特征，请稍候..."):
                    try:
                        # 将上传的图片保存为临时文件
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
                            tmp_file.write(uploaded_file.getvalue())
                            st.session_state.tmp_path = tmp_file.name

                        # 调用AI函数并流式输出
                        response_gen = analyze_geology_image(
                            image_path=st.session_state.tmp_path,
                            user_prompt=geo_prompt,
                            api_key=api_key,
                            model_name=model_choice
                        )

                        # 展示打字机效果并保存完整结果
                        full_response = st.write_stream(response_gen)
                        st.session_state.ai_result = full_response

                        # 用完删除临时文件，避免垃圾文件堆积
                        if st.session_state.tmp_path and os.path.exists(st.session_state.tmp_path):
                            os.unlink(st.session_state.tmp_path)
                            st.session_state.tmp_path = None

                        st.rerun()

                    except Exception as e:
                        st.error(f"❌ 解译失败: {str(e)}")

# ================= 5. 底部导出功能 =================
st.markdown("---")
# 只有上传图片+生成结果后，才显示导出按钮
if uploaded_file is not None and st.session_state.ai_result:
    if st.button("📥 导出标准化地质报告 (Word)", use_container_width=True):
        st.balloons()
        st.toast("🎉 报告生成模块已就绪，正式版将直接生成.docx文件！", icon="📄")