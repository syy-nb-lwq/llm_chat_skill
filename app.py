"""Streamlit Web UI - 流式输出版本"""
import streamlit as st
import os
from agent import StreamingAgent

st.set_page_config(page_title="AI Agent", page_icon="🤖", layout="wide")

# 初始化 session state
if "agent" not in st.session_state:
    st.session_state.agent = StreamingAgent()
if "messages" not in st.session_state:
    st.session_state.messages = []
if "uploaded_files" not in st.session_state:
    st.session_state.uploaded_files = {}

st.title("🤖 AI Agent - 流式对话")

# 侧边栏 - 设置
with st.sidebar:
    st.header("设置")
    if st.button("重置对话"):
        st.session_state.agent.reset()
        st.session_state.messages = []
        st.rerun()
    
    st.divider()
    
    # 技能列表
    st.subheader("📦 技能库")
    from tools.skill import get_skill_store
    store = get_skill_store()
    skills = store.list_valid()
    if skills:
        for s in skills:
            st.text(f"• {s.name}: {s.description[:30]}...")
    else:
        st.text("暂无技能")

# 主对话区域
st.header("💬 对话")

# 显示对话历史
for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    else:
        with st.chat_message("assistant"):
            st.markdown(msg["content"])

# 文件上传
st.divider()
st.subheader("📎 添加附件（可选）")

col1, col2 = st.columns([3, 1])
with col1:
    uploaded_file = st.file_uploader(
        "上传文件",
        type=["pdf", "txt", "md", "py", "json", "jpg", "png"],
        key="file_uploader"
    )
with col2:
    st.write("")  # 占位

# 输入框
user_input = st.chat_input("输入你的问题...")

# 处理输入
if user_input or uploaded_file:
    # 处理上传的文件
    file_info = ""
    if uploaded_file:
        upload_dir = "uploads"
        os.makedirs(upload_dir, exist_ok=True)
        
        import uuid
        file_ext = os.path.splitext(uploaded_file.name)[1]
        unique_name = f"{uuid.uuid4().hex}{file_ext}"
        file_path = os.path.join(upload_dir, unique_name)
        
        with open(file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        
        st.session_state.uploaded_files[uploaded_file.name] = file_path
        file_info = f"\n\n[上传文件: {file_path}]"
        
        st.session_state.pop("file_uploader")
    
    # 构建输入
    full_input = user_input + file_info if user_input else file_info
    
    # 添加用户消息
    st.session_state.messages.append({"role": "user", "content": full_input})
    
    with st.chat_message("user"):
        st.markdown(full_input)
    
    # 使用流式输出
    with st.chat_message("assistant"):
        status_placeholder = st.empty()
        result_placeholder = st.empty()
        
        collected_output = {"thinking": "", "result": ""}
        final_result = {"text": ""}
        
        def stream_callback(text: str):
            collected_output["thinking"] += text
            # 实时更新显示
            status_placeholder.markdown(f"<pre style='color: #888; font-size: 12px;'>{collected_output['thinking']}</pre>", unsafe_allow_html=True)
        
        # 调用 agent
        response = st.session_state.agent.chat_streaming(full_input, callback=stream_callback)
        
        # 最终结果显示
        final_result["text"] = response
        
        # 替换思考过程为最终结果
        result_placeholder.markdown(response)
        
        # 添加到消息历史
        st.session_state.messages.append({"role": "assistant", "content": response})
