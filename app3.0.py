import base64
import io
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime

import streamlit as st
from PIL import Image, ImageStat

try:
    from docx import Document
    from docx.shared import Inches
except Exception:
    Document = None
    Inches = None


APP_VERSION = "V3.0.0"
MEMORY_FILE = "geo_memory.jsonl"


# ================= 全局初始化：创建轻量记忆库，方便后续做错误案例和专家修正沉淀 =================
if not os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        f.write("")


# ================= 页面配置 =================
st.set_page_config(
    page_title="地质AI辅助解译系统",
    page_icon="🪨",
    layout="wide",
)


# ================= 记忆库与工具函数 =================
def load_memory_examples(limit=3):
    """读取最近几条记忆，用于提示词增强。"""
    examples = []
    if not os.path.exists(MEMORY_FILE):
        return examples
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                examples.append(json.loads(line))
            except Exception:
                continue
    return examples[-limit:]



def save_memory_record(record):
    """把本次解译结果与人工修正写入轻量记忆库。"""
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")



def get_image_quality(image_bytes):
    """基础图片质量控制，帮助系统先判断是否值得精细解译。"""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size
    stat = ImageStat.Stat(img)
    brightness = sum(stat.mean) / 3
    sharpness_proxy = sum(stat.var) / 3
    return {
        "图像宽度": width,
        "图像高度": height,
        "平均亮度": round(brightness, 2),
        "清晰度": round(sharpness_proxy, 2),
        "图像太小": width < 800 or height < 600,
        "图像太暗": brightness < 35,
        "图像太亮": brightness > 220,
        "图像太模糊": sharpness_proxy < 250,
    }



def classify_image_type_prompt():
    """返回统一的图像分类与解译要求。"""#规则说明书
    return """
你是地质技能大赛野外AI赛道助手。请先判断图像类型,再输出结构化结果。

图像类型可取值：
- 无人机航拍宏观地质影像
- 地面近景地质影像
- 宏微观混合地质影像
- 地质剖面图
- 图像类型不明

要求：
1. 先输出一段自然语言，且第一句必须是“这是一张……图。”
2. 再输出严格 JSON,方便程序解析。
3. JSON 中必须包含以下字段（每一项要换行输出）：
   - 图像类型
   - 图像质量评估
   - 可见要素
   - 推荐任务
   - 不适合任务
   - 自然语言解译
   - 结构化解译
   - 置信度
   - 不确定性
   - 下一步建议
4. 如果是 无人机航拍宏观地质图，请重点关注地形、地貌单元、地层展布、线性构造、沟谷水系，岩性分布
5. 如果是 地面实拍微观地质图，请重点关注岩性、结构构造、粒度、层理、节理裂隙、风化。
6. 如果是 宏微观混合地质影像，请分别给出宏观和微观两个分析块。
7. 如果是 地质剖面图，请重点关注地层和岩层特征、地质构造、岩浆活动、岩性、地质历史。
8. 如果图像质量差，请明确指出限制，不要强行下结论。
9. 语言要专业、克制、符合比赛规则，不要编造不可见信息。
10. 不要输出 Markdown。
""".strip()



def build_prompt(memory_examples, user_focus):
    """把记忆库和本次任务合成提示词。"""
    memory_text = "暂无历史记忆。"
    if memory_examples:
        memory_text = "\n".join(
            [
                f"- 案例{idx + 1}：输入特征={item.get('summary', '无')}；最终结论={item.get('final_conclusion', '无')}"
                for idx, item in enumerate(memory_examples)
            ]
        )

    return f"""
{classify_image_type_prompt()}

历史记忆参考：
{memory_text}

本次用户关注点：{user_focus}

补充要求：
- 自然语言解译必须是适合直接展示给用户的中文自然语言。
- 结构化解译必须是 JSON 对象，按图像类型动态组织字段。
- 如果是 无人机航拍宏观地质影像，结构化解译中建议包含：地形地貌、地层展布、线性构造、沟谷水系、岩性分布。
- 如果是 地面近景地质影像，结构化解译中建议包含：岩性、结构构造、粒度、层理、节理裂隙、风化。
- 如果是 地质剖面图，结构化解译中建议包含：地层接触关系、产状、褶皱、断层、岩体接触、时代关系。
- 如果是 宏微观混合地质影像，结构化解译中建议包含：宏观和微观两个分析块。
- 置信度用 0 到 1 之间的小数表示。
""".strip()



def extract_json_from_text(text):
    """尽量从模型输出中提取 JSON,容错处理多余文本。"""
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
    return None


def prepare_image_bytes(image_bytes, filename):
    """将 TIFF 等格式规范化为 JPEG，供预览和视觉模型稳定使用。"""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if image.width > 2048 or image.height > 2048:
        image.thumbnail((2048, 2048))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue(), f"{os.path.splitext(filename)[0]}.jpg"


def build_multiview_prompt(memory_examples, user_focus, photo_records):
    """构造多视角综合判读提示词，要求明确证据来源和待核验项。"""
    memory_text = "暂无历史记忆。"
    if memory_examples:
        memory_text = "\n".join(
            f"- 案例{index + 1}：{item.get('summary', '无')}"
            for index, item in enumerate(memory_examples)
        )
    photo_text = "\n".join(
        f"照片{item['编号']}（{item['文件名']}）：质量={json.dumps(item['图像质量评估'], ensure_ascii=False)}；"
        f"单图解译={json.dumps(item['单图解译'], ensure_ascii=False)}"
        for item in photo_records
    )
    return f"""
你是地质技能大赛 AI 赛道的多视角地质现象综合解译专家。
请基于同一地质现象的多张照片进行交叉核验，只能使用提供资料中可见或明确记录的证据。

历史案例参考：
{memory_text}

本次关注点：{user_focus}

单图解译资料：
{photo_text}

请先以简洁中文给出综合说明，随后输出严格 JSON。JSON 必须包括：
- 图像类型：本案例的主要图像类型
- 图像质量评估：总体质量与限制
- 可见要素：跨照片重复出现的直接可见事实
- 推荐任务
- 不适合任务
- 自然语言解译
- 结构化解译：包含岩性、构造特征、接触关系或地质现象（仅填写证据支持的项目）
- 证据链：数组，每项包含“结论”“证据照片编号”“可见证据”“可靠性”
- 冲突或待核验项：数组，列出照片间不一致或证据不足的判断
- 置信度：0 到 1 的小数
- 不确定性
- 下一步建议

规则：
1. “证据照片编号”只能引用提供的照片编号。
2. 区分直接可见事实和解释结论；无充分证据时写“待核验”，不得臆断断层性质、地层时代或精确岩性。
3. 若有效照片少于 5 张，必须在不确定性中说明多视角证据不足。
4. 不要输出 Markdown。
""".strip()


def make_mock_result(image_quality):
    """离线模式下返回标准化结果，便于比赛现场演示。"""
    return {
        "图像类型": "地面近景地质影像",
        "图像质量评估": image_quality,
        "可见要素": ["岩面", "节理裂隙", "轻微风化面"],
        "推荐研究任务": ["岩性识别", "风化特征识别", "构造特征识别"],
        "不适合研究任务": ["精确层位追踪", "高精度产状测量"],
        "自然语言解译": "这是一张地面实拍微观地质图。图像更适合观察岩性、风化面、节理裂隙和局部构造特征，但由于缺少尺度参照，细粒度判断仍需结合补拍照片和野外背景信息。",
        "结构化解译": {
            "岩性": "中细粒花岗岩（模拟示例）",
            "结构构造": "中细粒花岗结构，块状构造",
            "矿物或成分": ["石英", "钾长石", "斜长石", "少量黑云母"],
            "风化程度": "表面轻微风化，节理裂隙较发育",
            "地质解译": "整体表现为酸性侵入岩特征，适合做岩性与风化识别示范。",
        },
        "置信度": 0.72,
        "不确定性": ["缺少比例尺", "缺少拍摄位置和区域地质背景"],
        "下一步建议": ["补充近景与远景各一张", "增加比例尺或地质锤参照", "记录拍摄位置与层位信息"],
        "模型原始输出": "",
    }


# ================= 本地模型调用：所有图片与文字均留在本机 =================
def stream_local_ollama(model_name, prompt, images=None):
    """调用本机 Ollama 的 HTTP 接口，不连接外网。"""
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt, "images": images or []}],
        "stream": True,
        "options": {"temperature": 0.2},
    }
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            for raw_line in response:
                item = json.loads(raw_line.decode("utf-8"))
                text = item.get("message", {}).get("content", "")
                if text:
                    yield text
    except urllib.error.URLError as exc:
        yield f"❌ 无法连接本机 Ollama：{exc}。请确认 Ollama 已启动并已下载模型。"
    except Exception as exc:
        yield f"❌ 本地模型调用异常：{exc}"


def analyze_geology_image(image_path, user_prompt, _api_key, model_name):
    """使用本机视觉模型对单张照片进行初判。"""
    try:
        with open(image_path, "rb") as image_file:
            image = base64.b64encode(image_file.read()).decode("utf-8")
    except FileNotFoundError:
        yield "❌ 找不到图片文件，请检查上传。"
        return
    yield from stream_local_ollama(model_name, user_prompt, [image])


def render_result(result):#把结构化结果渲染成适合比赛展示的页面。
    """把结构化结果渲染成适合比赛展示的页面。"""
    st.subheader("3. 结构化解译结果")
    if not result:#检查结果
        st.info("请先完成一次解译。")
        return

    if result.get("error"):#检查错误
        st.error(result["error"])
        if result.get("raw_text"):#检查原始输出
            st.text_area("模型原始输出", result["raw_text"], height=180)
        return

    col_a, col_b, col_c = st.columns(3)#创建三个指标展示置信度、图像类型、推荐任务数
    col_a.metric("置信度", f"{round(result.get('置信度', 0) * 100)}%")
    col_b.metric("图像类型", result.get("图像类型", "图像类型不明"))
    col_c.metric("任务数", len(result.get("推荐任务", [])))

    st.text_area("自然语言解译\n" + result.get("自然语言解译", ""), height=120)
    evidence_chain = result.get("证据链", [])
    if evidence_chain:
        st.write("证据链（结论必须可回溯至照片编号）")
        st.dataframe(evidence_chain, use_container_width=True)
    verification_items = result.get("冲突或待核验项", [])
    if verification_items:
        st.warning("待核验项：" + "；".join(map(str, verification_items)))
    st.write("可见要素")
    st.write(result.get("可见要素", []))
    st.write("推荐任务")
    st.write(result.get("推荐任务", []))
    st.write("不适合任务")
    st.write(result.get("不适合任务", []))
    st.write("不确定性")
    st.write(result.get("不确定性", []))
    st.write("下一步建议")
    st.write(result.get("下一步建议", []))

    with st.expander("查看底层结构化结果 JSON"):#查看底层结构化结果JSON（给专业人士或调试人员看的）
        st.json(result.get("结构化解译", {}))



def export_docx(image_bytes, result, output_path):#导出Word报告
    """导出 Word 报告；如果缺少依赖，则返回友好提示。"""
    if Document is None:#检查依赖
        return False, "当前环境缺少 python-docx，无法生成 .docx。"

    doc = Document()#创建Word文档
    doc.add_heading("地质AI辅助解译报告", level=1)#添加标题
    doc.add_paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")#添加生成时间
    doc.add_paragraph(f"系统版本：{APP_VERSION}")#添加系统版本

    doc.add_heading("一、图像信息", level=2)
    doc.add_paragraph(f"图像类型：{result.get('图像类型', '图像类型不明')}")
    iq = result.get("图像质量评估", {})#获取图像质量评估
    if not isinstance(iq, dict):
        iq = {}#如果图像质量评估不是字典，则设置为空字典
    width = iq.get("图像宽度", iq.get("width", "NA"))#获取图像宽度
    height = iq.get("图像高度", iq.get("height", "NA"))#获取图像高度
    doc.add_paragraph(f"图像宽高：{width} × {height}")

    if image_bytes and Inches is not None:#检查图片
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(image_bytes)
            tmp_img_path = tmp.name
        try:#插入图片
            doc.add_picture(tmp_img_path, width=Inches(5.8))
        finally:#清理临时文件
            if os.path.exists(tmp_img_path):#删除临时文件
                os.unlink(tmp_img_path)

    doc.add_heading("二、自然语言结论", level=2)#添加自然语言结论
    doc.add_paragraph(result.get("自然语言解译", ""))

    doc.add_heading("三、结构化解译结果", level=2)#添加结构化解译结果
    structured = result.get("结构化解译", {})#获取结构化解译结果
    if isinstance(structured, dict):#如果结构化解译结果是字典
        for key, value in structured.items():#遍历结构化解译结果
            if isinstance(value, list):
                value_text = ", ".join(map(str, value))#如果结构化解译结果是列表，则将列表转换为字符串
            elif isinstance(value, dict):
                value_text = json.dumps(value, ensure_ascii=False)#如果结构化解译结果是字典，则将字典转换为字符串
            else:
                value_text = str(value)#如果结构化解译结果是其他类型，则将结果转换为字符串
            doc.add_paragraph(f"{key}：{value_text}")
    else:
        doc.add_paragraph(str(structured))#如果结构化解译结果不是字典，则将结果转换为字符串

    doc.add_heading("四、置信度与不确定性", level=2)
    doc.add_paragraph(f"置信度：{result.get('置信度', 0)}")#添加置信度

    uncertainty = result.get("不确定性", [])#获取不确定性
    if isinstance(uncertainty, list):
        uncertainty_text = ", ".join(map(str, uncertainty))#如果不确定性是列表，则将列表转换为字符串
    elif uncertainty is None:
        uncertainty_text = ""#如果不确定性是None，则设置为空字符串
    else:
        uncertainty_text = str(uncertainty)#如果不确定性是其他类型，则将结果转换为字符串    

    suggestions = result.get("下一步建议", [])
    if isinstance(suggestions, list):
        suggestions_text = ", ".join(map(str, suggestions))
    elif suggestions is None:
        suggestions_text = ""
    else:
        suggestions_text = str(suggestions)

    doc.add_paragraph(f"不确定性：{uncertainty_text}")
    doc.add_paragraph(f"建议：{suggestions_text}")

    doc.save(output_path)#保存文档
    return True, output_path#返回成功和路径 (Word报告路径)


def image_bytes_as_jpeg(image_bytes):
    """将 JPG、PNG、TIFF 等输入统一转换为模型可读取的 JPEG。"""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue()


def analyze_multiview_images(image_items, user_prompt, _api_key, model_name):
    """把同一地质现象的多张照片提交给本机视觉模型。"""
    images = []
    for item in image_items:
        try:
            images.append(base64.b64encode(image_bytes_as_jpeg(item["bytes"])).decode("utf-8"))
        except Exception as exc:
            yield f"❌ 无法处理图片 {item['name']}：{exc}"
            return
    yield from stream_local_ollama(model_name, user_prompt, images)


def build_multiview_prompt(user_focus, single_results):
    """生成带有证据链约束的多视角综合解译提示词。"""
    result_text = json.dumps(single_results, ensure_ascii=False, indent=2)
    return f"""
你是地质技能大赛 AI 赛道的综合解译专家。以下图片属于同一个地质现象，请综合所有视角，不要把单张照片的猜测直接当成最终结论。

用户关注点：{user_focus}
单图初判结果（仅作参考，必须回看图片核验）：
{result_text}

请先输出一句简洁的综合结论，再输出严格 JSON，不要使用 Markdown。JSON 必须包含：
- 案例类型
- 综合结论
- 观测事实：只能写图片中直接可见的内容
- 地质解释：岩性、结构构造、地质现象及其依据
- 证据链：每条结论必须列出支持它的照片编号
- 跨视角一致性
- 冲突证据
- 置信度：0 到 1 的小数
- 不确定性
- 待人工核验项
- 剖面图表达建议

严格区分“观测事实”“地质解释”和“待验证假设”。缺少尺度、产状或空间位置时必须明确说明，不能编造不可见信息。
""".strip()


# ================= 主界面 =================
st.title("🪨 地质AI辅助解译与成图系统")
st.caption("适合比赛演示的可用原型：保留流式输出、结构化结果、人工修正和 Word 导出。")
st.markdown("---")

# 侧边栏设置
st.sidebar.title("⚙️ 系统设置")
local_base_url = "http://127.0.0.1:11434"
model_choice = st.sidebar.selectbox("选择本地视觉模型", ["qwen3-vl:8b"])
user_focus = st.sidebar.text_area("本次关注点", "请在此输入您本次的解译关注点")
st.sidebar.info(f"当前系统版本：{APP_VERSION}\n本地 Ollama 模式：不使用外网")

col1, col2 = st.columns([1, 1.25])

with col1:
    st.subheader("📥 1. 数据输入")
    st.caption("请将同一地质现象的照片作为一个案例上传，建议至少提供 5 个不同视角。")
    uploaded_files = st.file_uploader(
        "上传同一地质现象的多视角照片",
        type=["jpg", "png", "jpeg", "tif", "tiff"],
        accept_multiple_files=True,
    )
    image_items = []
    image_quality = {}

    if uploaded_files:
        for index, uploaded in enumerate(uploaded_files, start=1):
            raw_bytes = uploaded.getvalue()
            try:
                quality = get_image_quality(raw_bytes)
                image_items.append({
                    "id": f"照片-{index:02d}",
                    "name": uploaded.name,
                    "bytes": raw_bytes,
                    "quality": quality,
                })
            except Exception as exc:
                st.warning(f"无法读取 {uploaded.name}：{exc}")

        st.info(f"已载入 {len(image_items)} 张照片。")
        if len(image_items) < 5:
            st.warning("当前照片数量少于 5 张，多视角证据不足，建议补充同一现象的其他角度。")
        preview_columns = st.columns(min(len(image_items), 5))
        for index, item in enumerate(image_items):
            with preview_columns[index % len(preview_columns)]:
                st.image(item["bytes"], caption=item["id"], use_container_width=True)
                with st.expander("质量", expanded=False):
                    st.json(item["quality"])
        image_quality = image_items[0]["quality"] if image_items else {}

    st.subheader("✏️ 4. 人工修正")
    manual_lithology = st.text_input("修正岩性", value=st.session_state.get("manual_lithology", ""))#修正岩性
    manual_comment = st.text_area("修正说明", value=st.session_state.get("manual_comment", ""), height=90)#修正说明
    if st.button("保存人工修正", use_container_width=True):#保存人工修正
        st.session_state.manual_lithology = manual_lithology
        st.session_state.manual_comment = manual_comment
        st.success("已保存人工修正，本次修正将进入记忆库。")

with col2:
    st.subheader("🤖 2. AI 智能解译")

    if "raw_stream_text" not in st.session_state:#初始化session state(会话状态)
        st.session_state.raw_stream_text = ""
    if "structured_result" not in st.session_state:
        st.session_state.structured_result = {}
    if "tmp_path" not in st.session_state:
        st.session_state.tmp_path = None

    if image_items:
        memory_examples = load_memory_examples(limit=3)
        single_prompt = build_prompt(memory_examples, user_focus)

        if st.button("🚀 开始多视角 AI 综合解译", type="primary", use_container_width=True):
            st.session_state.raw_stream_text = ""
            st.session_state.structured_result = {}

            with st.spinner("AI 正在逐图初判并进行多视角综合解译，请稍候..."): 
                    single_results = []
                    for item in image_items:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
                            tmp_file.write(image_bytes_as_jpeg(item["bytes"]))
                            single_path = tmp_file.name
                        try:
                            single_text = "".join(analyze_geology_image(single_path, single_prompt, local_base_url, model_choice))
                            single_results.append({
                                "照片编号": item["id"],
                                "文件名": item["name"],
                                "图像质量": item["quality"],
                                "初判": extract_json_from_text(single_text) or {"模型原始输出": single_text},
                            })
                        finally:
                            if os.path.exists(single_path):
                                os.unlink(single_path)

                    combined_prompt = build_multiview_prompt(user_focus, single_results)
                    full_text = st.write_stream(analyze_multiview_images(image_items, combined_prompt, local_base_url, model_choice))
                    st.session_state.raw_stream_text = full_text or ""
                    parsed = extract_json_from_text(st.session_state.raw_stream_text)
                    st.session_state.structured_result = parsed or {
                        "error": "模型输出中未解析到有效 JSON",
                        "模型原始输出": st.session_state.raw_stream_text,
                    }
                    st.session_state.structured_result["单图初判"] = single_results

            if st.session_state.structured_result and not st.session_state.structured_result.get("error"):
                st.session_state.structured_result["manual_lithology"] = st.session_state.get("manual_lithology", "")
                st.session_state.structured_result["manual_comment"] = st.session_state.get("manual_comment", "")
                st.session_state.structured_result["image_quality_assessment"] = image_quality or {}
                save_memory_record({
                    "timestamp": datetime.now().isoformat(),
                    "summary": (st.session_state.structured_result.get("综合结论") or "")[:120],
                    "final_conclusion": json.dumps(st.session_state.structured_result, ensure_ascii=False),
                    "manual_lithology": st.session_state.get("manual_lithology", ""),
                    "manual_comment": st.session_state.get("manual_comment", ""),
                })
                st.success("多视角解译完成，结果已写入轻量记忆库。")

        if st.session_state.raw_stream_text:
            st.subheader("3. 多视角综合解译原文")
            st.text_area("实时生成内容", st.session_state.raw_stream_text, height=180)

        if st.session_state.structured_result:
            render_result(st.session_state.structured_result)
            if st.session_state.structured_result.get("证据链"):
                st.subheader("证据链核验")
                st.json(st.session_state.structured_result["证据链"])
            if st.session_state.structured_result.get("单图初判"):
                with st.expander("查看逐图初判结果"):
                    st.json(st.session_state.structured_result["单图初判"])

# ================= 导出 Word 报告 =================
st.markdown("---")
if image_items and st.session_state.structured_result and not st.session_state.structured_result.get("error"): 
    st.subheader("5. 报告导出")
    if st.button("📥 导出标准化地质报告 (Word)", use_container_width=True):
        with st.spinner("正在生成 Word 报告..."):
            tmp_docx = os.path.join(
                tempfile.gettempdir(),
                f"geology_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx",
            )
            ok, result = export_docx(image_items[0]["bytes"], st.session_state.structured_result, tmp_docx)
            if ok:
                with open(result, "rb") as f:
                    st.download_button(
                        "点击下载 Word 报告",
                        data=f.read(),
                        file_name=os.path.basename(result),
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        use_container_width=True,
                    )
                st.toast("Word 报告已生成，可以下载。", icon="📄")
            else:
                st.warning(result)
