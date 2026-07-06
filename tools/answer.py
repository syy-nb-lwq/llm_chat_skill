"""问答工具"""
from openai import OpenAI
from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL


def answer_from_page(url: str, question: str, page_text: str = None) -> dict:
    """
    根据用户问题，从页面内容中提取/生成答案。

    Args:
        url: 网页 URL（用于上下文）
        question: 用户的问题
        page_text: 可选，如果已经抓取过页面，可以直接传入文本

    Returns:
        {
            "success": bool,
            "answer": str,      # 回答内容
            "error": str        # 错误信息
        }
    """
    if not question:
        return {"success": False, "answer": "", "error": "question 不能为空"}

    # 如果没有传入文本，需要先抓取
    if page_text is None:
        from tools.fetch import fetch_webpage
        result = fetch_webpage(url)
        if not result["success"]:
            return {"success": False, "answer": "", "error": result["error"]}
        page_text = result["text"]

    prompt = f"""你是一个专业的阅读理解助手。请根据以下网页内容回答用户的问题。

网页 URL: {url}

用户问题: {question}

要求：
1. 只基于网页内容回答，不要添加网页中没有的信息
2. 如果网页内容无法回答问题，请明确说明"网页中没有相关信息"
3. 回答要准确、简洁
4. 如果需要，可以引用网页中的原文来支持回答

网页内容：
{page_text[:30000]}
"""

    try:
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个专业的阅读理解助手。请基于网页内容准确回答问题。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=2000
        )

        answer = response.choices[0].message.content.strip()
        return {"success": True, "answer": answer, "error": None}

    except Exception as e:
        return {"success": False, "answer": "", "error": str(e)}
