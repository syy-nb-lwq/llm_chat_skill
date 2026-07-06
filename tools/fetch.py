"""网页抓取"""
import requests
import trafilatura
from bs4 import BeautifulSoup


def fetch_webpage(url: str) -> dict:
    """抓取网页内容"""
    try:
        # 方式1: trafilatura（推荐，自动提取正文）
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, output_format='markdown', 
                                       include_tables=True, include_images=False)
            return {
                "success": True,
                "url": url,
                "title": _extract_title(downloaded) or "",
                "text": text or downloaded[:5000],
                "source": "trafilatura"
            }
        
        # 方式2: requests + BeautifulSoup
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml',
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        html = response.text
        text = trafilatura.extract(html, output_format='markdown', include_tables=True)
        
        if not text:
            soup = BeautifulSoup(html, 'html.parser')
            for script in soup(["script", "style"]):
                script.decompose()
            text = soup.get_text(separator='\n', strip=True)[:5000]
        
        return {
            "success": True,
            "url": url,
            "title": _extract_title_from_html(html) or "",
            "text": text,
            "source": "requests"
        }
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            return {
                "success": False,
                "url": url,
                "error": "403 Forbidden - 该网站限制爬虫访问"
            }
        return {"success": False, "url": url, "error": str(e)}
    except Exception as e:
        return {"success": False, "url": url, "error": str(e)}


def _extract_title(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.find('title')
    return title.get_text(strip=True) if title else ""


def _extract_title_from_html(html: str) -> str:
    return _extract_title(html)


if __name__ == "__main__":
    result = fetch_webpage("https://example.com")
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Title: {result.get('title', '')}")
        print(f"Text: {result.get('text', '')[:200]}...")
