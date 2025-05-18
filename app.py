import requests
from bs4 import BeautifulSoup
import json
from urllib.parse import urljoin, urlparse
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl
from typing import List, Dict, Optional, Any
import uvicorn
import logging
import re
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# Отключаем предупреждения о небезопасных SSL-соединениях
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("news_parser")

app = FastAPI(
    title="News Parser API",
    description="API для парсинга новостей с различных сайтов",
    version="1.0.0"
)

class SiteList(BaseModel):
    urls: List[str]

class NewsItem(BaseModel):
    url: str
    title: str
    content: str

class NewsResponse(BaseModel):
    total: int
    news: List[NewsItem]

def fetch_page(url: str) -> Optional[str]:
    """Загружает HTML-страницу с отключенной проверкой SSL для проблемных сайтов."""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=False)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Ошибка при загрузке {url}: {e}")
        return None

def detect_news_links(url: str, max_links: int = 10) -> List[str]:
    """Определяет ссылки на новостные статьи на основе структуры страницы."""
    html = fetch_page(url)
    if not html:
        return []
    
    soup = BeautifulSoup(html, 'lxml')
    news_links = set()
    parsed_url = urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    
    # Исключаем ненужные страницы и навигационные элементы
    exclude_patterns = [
        "/tag/", "/tags/", "/contacts", "/contact", "/about", "/sotrudnichestvo", 
        "/view_file.pdf", "/search", "/login", "/register", "/auth", 
        "/rss", "/advertising", "/privacy", "/terms", ".jpg", ".png", ".pdf",
        "/category/", "/categories/", "/section/", "/sections/", "/topics/",
        "/feed/", "/subscriptions/", "/subscribe/", "/newsletter", "/newsletters/",
        "/latest/", "/popular/", "/archive/", "/authors/", "/team/", 
        "?utm_source=", "?from=", "/pages/", "/help/", "/support/", "/dc/", 
        "/newspaper/", "/crypto/", "/industries/", "/gorod/"
    ]
    
    # Наиболее вероятные шаблоны URL для новостей
    news_patterns = [
        r"/\d{4}/\d{1,2}/\d{1,2}/",  # URL с датой
        r"/news/\d+",                # URL с ID новости
        r"/article/\d+",             # URL с ID статьи
        r"/\d{4,}/\d{1,2}/\d{1,2}/[a-z0-9-]+",  # Дата + слаг
        r"/news/[a-z0-9-]+",         # /news/ + слаг
        r"/article/[a-z0-9-]+",      # /article/ + слаг
        r"/post/[a-z0-9-]+",         # /post/ + слаг
        r"/story/[a-z0-9-]+",        # /story/ + слаг
        r"/[a-z0-9-]{10,}",          # Длинный слаг (вероятно новость)
    ]
    
    # Попытка найти контейнеры с новостями
    news_containers = []
    
    # Типичные селекторы для новостных контейнеров
    for selector in [
        "article", "div.article", "div.news", "div.post", "li.news-item", 
        ".news-list", ".article-list", ".post-list", ".news-feed", 
        ".news-container", ".articles-container", ".posts-container",
        "[data-testid='latest-stream']", "[data-testid='article-stream']"
    ]:
        containers = soup.select(selector)
        if containers:
            news_containers.extend(containers)
    
    # Если не нашли специфические контейнеры, используем более общий поиск
    if not news_containers:
        # Ищем div с классами, содержащими ключевые слова
        for div in soup.find_all("div", class_=True):
            class_attr = " ".join(div["class"])
            if any(keyword in class_attr.lower() for keyword in ["news", "article", "post", "story", "entry"]):
                news_containers.append(div)
    
    links_to_check = []
    
    # Если нашли контейнеры с новостями, ищем ссылки в них
    if news_containers:
        for container in news_containers:
            # Проверяем, есть ли h-теги рядом с ссылками (часто указывает на заголовок новости)
            for heading in container.find_all(["h1", "h2", "h3", "h4"]):
                link = heading.find("a", href=True)
                if link:
                    links_to_check.append(link)
            
            # Ищем ссылки с текстом (вероятно заголовки)
            for link in container.find_all("a", href=True):
                if link.get_text(strip=True) and len(link.get_text(strip=True)) > 15:
                    links_to_check.append(link)
    
    # Если не нашли специфические контейнеры или ссылки, используем другие методы
    if not links_to_check:
        # Ищем ссылки внутри заголовков
        for heading in soup.find_all(["h2", "h3"]):
            link = heading.find("a", href=True)
            if link:
                links_to_check.append(link)
        
        # Ищем ссылки с большим текстовым содержимым (вероятно заголовки)
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            if text and len(text) > 30 and len(text) < 150:  # Типичная длина заголовка
                links_to_check.append(link)
    
    # Дедупликация списка ссылок
    unique_links = set()
    for link in links_to_check:
        if link not in unique_links:
            unique_links.add(link)
    links_to_check = list(unique_links)
    
    # Обрабатываем все найденные ссылки
    for link in links_to_check:
        href = link['href']
        
        # Пропускаем пустые ссылки или якори
        if not href or href.startswith('#') or href.startswith('javascript:'):
            continue
            
        full_url = urljoin(base_url, href)
        parsed_href = urlparse(full_url)
        
        # Пропускаем ссылки на другие домены
        if parsed_url.netloc not in parsed_href.netloc:
            continue
            
        # Пропускаем навигационные элементы и рубрики
        if any(pattern in full_url.lower() for pattern in exclude_patterns):
            continue
        
        # Проверяем соответствие URL паттернам новостей
        path = parsed_href.path
        is_likely_news = False
        
        # Проверяем по регулярным выражениям
        for pattern in news_patterns:
            if re.search(pattern, path):
                is_likely_news = True
                break
                
        # Дополнительные эвристики для определения новостей
        if not is_likely_news:
            # Количество сегментов в пути URL (обычно для новостей больше 2-3)
            segments = [s for s in path.split('/') if s]
            if len(segments) >= 2:
                # Последний сегмент обычно длинный для новостей (slug)
                if segments and len(segments[-1]) > 10:
                    is_likely_news = True
                # Проверяем наличие цифр в сегментах (часто ID или даты)
                elif any(any(c.isdigit() for c in segment) for segment in segments):
                    is_likely_news = True
        
        # Проверяем текст ссылки (обычно заголовок новости)
        link_text = link.get_text(strip=True)
        if link_text and len(link_text) > 30 and len(link_text) < 200:
            # Обычно заголовки новостей содержат глаголы или двоеточия
            if ":" in link_text or any(word in link_text.lower() for word in ["says", "claims", "reports", "announced", "revealed", "launches", "introduces"]):
                is_likely_news = True
        
        if is_likely_news and full_url not in news_links:
            news_links.add(full_url)
            logger.info(f"Найдена новостная ссылка: {full_url}")
            
        # Ограничиваем количество ссылок
        if len(news_links) >= max_links:
            break
    
    # Если не удалось найти новости через селекторы, используем последнее средство - URL heuristics
    if not news_links:
        logger.warning(f"Не удалось найти новости через селекторы для {url}, использую URL-эвристики")
        all_links = soup.find_all("a", href=True)
        
        for link in all_links:
            href = link['href']
            if not href or href.startswith('#') or href.startswith('javascript:'):
                continue
                
            full_url = urljoin(base_url, href)
            parsed_href = urlparse(full_url)
            
            # Проверяем, что ссылка ведет на тот же домен
            if parsed_url.netloc not in parsed_href.netloc:
                continue
                
            # Пропускаем навигационные элементы
            if any(pattern in full_url.lower() for pattern in exclude_patterns):
                continue
            
            # Проверяем соответствие URL паттернам новостей
            for pattern in news_patterns:
                if re.search(pattern, parsed_href.path):
                    if full_url not in news_links:
                        news_links.add(full_url)
                        logger.info(f"Найдена новостная ссылка по паттерну: {full_url}")
                        break
            
            # Ограничиваем количество ссылок
            if len(news_links) >= max_links:
                break
    
    return list(news_links)

def parse_news_content(url: str) -> Optional[Dict[str, str]]:
    """Извлекает заголовок и текст новости без мусорных данных."""
    html = fetch_page(url)
    if not html:
        return None
    
    soup = BeautifulSoup(html, 'lxml')
    
    # Поиск заголовка
    title = None
    title_candidates = [
        soup.find('h1'),
        soup.select_one('.news-title, .article-title, .post-title, .entry-title, .headline, .title'),
        soup.find('meta', property='og:title'),
        soup.find('meta', {'name': 'title'}),
        soup.find('title')
    ]
    
    for candidate in title_candidates:
        if candidate:
            if hasattr(candidate, 'get'):
                title = candidate.get('content', '')
            else:
                title = candidate.get_text(strip=True)
            if title:
                break
    
    if not title:
        title = "Без заголовка"
    
    # Поиск содержимого новости
    content_text = ""
    
    # 1. Попытка найти основной контейнер с контентом
    content_containers = [
        soup.select_one('article, .article, .news-content, .post-content, .entry-content, .content, .article-body, .post-body, .story-body'),
        soup.find('div', {'itemprop': 'articleBody'}),
        soup.find('div', class_=lambda c: c and any(x in c for x in ['content', 'article', 'news', 'text', 'body', 'story'])),
        soup.find('div', id=lambda i: i and any(x in i for x in ['content', 'article', 'news', 'text', 'body', 'story']))
    ]
    
    for container in content_containers:
        if container:
            # Исключаем навигационные элементы
            for nav in container.select('nav, .nav, .navigation, .share, .social, .related, .comments, .sidebar, aside, footer, header'):
                if nav:
                    nav.decompose()
            
            # Извлекаем текст из абзацев
            paragraphs = container.find_all('p')
            if paragraphs:
                filtered_paragraphs = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20]
                if filtered_paragraphs:
                    content_text = " ".join(filtered_paragraphs)
                    break
    
    # 2. Если не найдено контейнеров, используем все абзацы внутри <main> или <body>
    if not content_text:
        main_content = soup.find('main') or soup.find('body')
        if main_content:
            # Исключаем навигационные элементы
            for nav in main_content.select('nav, .nav, .navigation, .share, .social, .related, .comments, .sidebar, aside, footer, header'):
                if nav:
                    nav.decompose()
                    
            paragraphs = main_content.find_all('p')
            filtered_paragraphs = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20]
            if filtered_paragraphs:
                content_text = " ".join(filtered_paragraphs)
    
    # 3. Последняя попытка: все абзацы на странице
    if not content_text:
        paragraphs = soup.find_all('p')
        filtered_paragraphs = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20]
        if filtered_paragraphs:
            content_text = " ".join(filtered_paragraphs)
    
    # Проверка качества контента
    if not content_text or len(content_text) < 100:  # Повышаем минимальную длину текста
        logger.warning(f"Недостаточно контента для {url}")
        return None
    
    # Проверяем признаки того, что это не новостная статья
    if any(phrase in content_text.lower() for phrase in ["404", "not found", "page not found", "страница не найдена"]):
        logger.warning(f"Вероятно, это страница ошибки: {url}")
        return None
    
    # Проверка на дубликаты абзацев (частая проблема на страницах с списком новостей)
    paragraphs_set = set(filtered_paragraphs) if 'filtered_paragraphs' in locals() else set()
    if len(paragraphs_set) < 3:  # Если уникальных абзацев очень мало
        logger.warning(f"Слишком мало уникальных абзацев для {url}, вероятно, это не статья")
        return None
    
    return {
        "url": url,
        "title": title,
        "content": content_text
    }

async def process_site(url: str, max_news: int = 5) -> List[Dict[str, str]]:
    """Обрабатывает один сайт и возвращает список новостей."""
    result = []
    try:
        logger.info(f"Парсинг сайта: {url}")
        news_links = detect_news_links(url, max_links=max_news * 2)  # Берем больше ссылок на случай, если некоторые не распарсятся
        
        if not news_links:
            logger.warning(f"Не удалось найти новостные ссылки на {url}")
            return []
        
        logger.info(f"Найдено {len(news_links)} потенциальных новостных ссылок на {url}")
        
        # Для каждой найденной ссылки пытаемся извлечь содержимое
        for news_url in news_links:
            logger.info(f"Обработка новости: {news_url}")
            news_data = parse_news_content(news_url)
            if news_data:
                result.append(news_data)
                logger.info(f"Успешно извлечено содержимое из {news_url}")
                
                # Если достигли нужного количества новостей, останавливаемся
                if len(result) >= max_news:
                    break
            else:
                logger.warning(f"Не удалось извлечь содержимое из {news_url}")
    
    except Exception as e:
        logger.error(f"Ошибка при обработке сайта {url}: {e}")
    
    logger.info(f"Всего извлечено {len(result)} новостей с сайта {url}")
    return result

@app.post("/parse_news", response_model=NewsResponse)
async def parse_news(site_list: SiteList):
    """Парсит новости с предоставленных сайтов и возвращает результаты."""
    if not site_list.urls:
        raise HTTPException(status_code=400, detail="Список URL-адресов не может быть пустым")
    
    all_news = []
    for site_url in site_list.urls:
        site_news = await process_site(site_url)
        all_news.extend(site_news)
    
    return NewsResponse(
        total=len(all_news),
        news=all_news
    )

@app.get("/")
async def root():
    """Корневой эндпоинт с информацией об API."""
    return {
        "name": "News Parser API",
        "version": "1.0.0",
        "description": "API для парсинга новостей с различных сайтов",
        "endpoints": {
            "/parse_news": "POST запрос для парсинга новостей с указанных сайтов"
        },
        "example": {
            "request": {
                "urls": ["https://example.com/news", "https://another-site.com/feed"]
            }
        }
    }

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)