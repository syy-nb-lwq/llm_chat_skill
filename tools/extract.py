"""结构化字段提取工具"""
from openai import OpenAI
from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL


def extract_field(url: str, fields: list[str], page_text: str = None) -> dict:
    """
    从网页中提取指定字段。

    Args:
        url: 网页 URL（用于上下文）
        fields: 要提取的字段列表，如 ["公司名", "CEO", "成立时间"]
        page_text: 可选，如果已经抓取过页面，可以直接传入文本

    Returns:
        {
            "success": bool,
            "data": dict,       # 提取的字段数据
            "error": str        # 错误信息
        }
    """
    if not fields:
        return {"success": False, "data": {}, "error": "fields 不能为空"}

    # 如果没有传入文本，需要先抓取
    if page_text is None:
        from tools.fetch import fetch_webpage
        result = fetch_webpage(url)
        if not result["success"]:
            return {"success": False, "data": {}, "error": result["error"]}
        page_text = result["text"]

    # 构建抽取 prompt
    fields_str = "\n".join([f"- {f}" for f in fields])
    prompt = f"""你是一个信息提取专家。请从以下网页内容中提取指定的字段。

网页 URL: {url}

要提取的字段：
{fields_str}

要求：
1. 只返回提取到的信息，不要添加解释
2. 如果某个字段在页面中找不到，标记为 null
3. 如果页面中没有相关内容，也要返回，尽量填充能找到的信息
4. 输出格式为 JSON

网页内容：
{page_text[:30000]}

请以 JSON 格式返回结果，格式如下：
{{"field1": "value1", "field2": "value2", ...}}
"""

    try:
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个专业的信息提取助手。请严格按要求提取信息，只返回 JSON 格式的结果。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=2000
        )

        result_text = response.choices[0].message.content.strip()

        # 尝试解析 JSON
        # 去掉可能的 markdown 代码块
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
        result_text = result_text.strip()

        import json
        data = json.loads(result_text)
        return {"success": True, "data": data, "error": None}

    except json.JSONDecodeError as e:
        return {"success": False, "data": {}, "error": f"JSON 解析失败: {str(e)}, 原始内容: {result_text[:200]}"}
    except Exception as e:
        return {"success": False, "data": {}, "error": str(e)}
