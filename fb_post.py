"""
fb_post.py - сваля текста и снимките от пост във Facebook.

Първо пускане (влизаш в акаунта си само веднъж, сесията се пази във fb_session.json):
    python fb_post.py --login

След това:
    python fb_post.py "https://www.facebook.com/.../posts/..."
    python fb_post.py линк1 линк2 линк3
    python fb_post.py -f links.txt          (по един линк на ред)
    python fb_post.py --headless "линк"     (без да се показва прозорец)

Резултатът е в папка downloads/<дата>_<id>/ : text.txt, 01.jpg, 02.jpg ..., info.json
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "fb_session.json"  # бисквитките за вход - не го давай на никого
OUT_DIR = HERE / "downloads"

MAIN_IMAGE = 'img[data-visualcompletion="media-vc-image"]'

# Facebook често сменя HTML-а си - ако текстът спре да се намира, тук се добавя нов селектор.
EXTRACT_JS = r"""
() => {
  const sels = [
    '[data-ad-rendering-role="story_message"]',
    '[data-ad-comet-preview="message"]',
    '[data-ad-preview="message"]',
  ];
  let msg = null;
  for (const s of sels) { msg = document.querySelector(s); if (msg) break; }

  const scope = (msg && (msg.closest('[role="article"]') || msg.closest('[role="dialog"]')))
    || document.querySelector('[role="dialog"]')
    || document.querySelector('[role="main"]')
    || document.body;

  const isPhoto = h => /\/photo(\.php)?\/?\?|\/photos\/|[?&]fbid=/.test(h);
  // Снимки в коментарите не ни трябват: коментарите са вложени role="article".
  const inComment = a => {
    const art = a.closest('[role="article"]');
    if (!art || art === scope) return false;
    return !!art.parentElement.closest('[role="article"]')
      || /comment|коментар/i.test(art.getAttribute('aria-label') || '');
  };

  const links = [];
  for (const a of scope.querySelectorAll('a[href]')) {
    if (!a.querySelector('img') || !isPhoto(a.href) || inComment(a)) continue;
    if (!links.includes(a.href)) links.push(a.href);
  }

  // При много снимки FB показва 4-5 и "+N" върху последната.
  let extra = 0;
  if (links.length) {
    const last = [...scope.querySelectorAll('a[href]')].filter(a => a.href === links[links.length - 1]).pop();
    const m = last && (last.innerText || '').match(/\+\s*(\d+)/);
    if (m) extra = parseInt(m[1], 10);
  }

  const og = document.querySelector('meta[property="og:description"]');
  return {
    text: msg ? msg.innerText.trim() : (og ? og.content.trim() : ''),
    links,
    extra,
  };
}
"""

SEE_MORE_JS = r"""
() => {
  const msg = document.querySelector('[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]');
  if (!msg) return false;
  for (const b of msg.querySelectorAll('[role="button"]')) {
    if (/^(see more|виж още|вижте още|още)$/i.test(b.innerText.trim())) { b.click(); return true; }
  }
  return false;
}
"""


def launch(p, headless):
    # Ползваме вече инсталиран браузър (Edge/Chrome), за да не се тегли Chromium.
    for channel in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(channel=channel, headless=headless)
        except PWError:
            continue
    sys.exit("Не намерих Edge или Chrome. Пусни: python -m playwright install chromium")


def new_context(browser):
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
    )


def logged_in(ctx):
    return any(c["name"] == "c_user" for c in ctx.cookies("https://www.facebook.com"))


def login(p):
    browser = launch(p, headless=False)
    ctx = new_context(browser)
    page = ctx.new_page()
    page.goto("https://www.facebook.com/")
    input("Влез в акаунта си в отворения прозорец, после натисни Enter тук...")
    if not logged_in(ctx):
        browser.close()
        sys.exit("Не виждам вход в акаунт - опитай пак.")
    ctx.storage_state(path=str(SESSION_FILE))
    browser.close()
    print(f"Готово, сесията е запазена във {SESSION_FILE.name}")


def post_id(url):
    q = parse_qs(urlparse(url).query)
    for key in ("story_fbid", "fbid", "v"):
        if key in q:
            return q[key][0]
    m = re.search(r"(pfbid\w+|\d{8,})", url) or re.search(r"/share/\w/(\w+)", url)
    return m.group(1) if m else "post"


def photo_key(url):
    q = parse_qs(urlparse(url).query)
    return q.get("fbid", [url])[0]


def main_image_src(page, previous=None, timeout=10):
    """Чака голямата снимка в photo viewer-а и връща адреса ѝ."""
    end = time.time() + timeout
    while time.time() < end:
        src = page.evaluate(
            """(sel) => {
                const imgs = [...document.querySelectorAll(sel)];
                if (imgs.length) return imgs[imgs.length - 1].src;
                // резервен вариант: най-голямата снимка на страницата
                let best = null;
                for (const i of document.images)
                    if (i.naturalWidth > 400 && (!best || i.naturalWidth > best.naturalWidth)) best = i;
                return best ? best.src : null;
            }""",
            MAIN_IMAGE,
        )
        if src and src != previous:
            return src
        time.sleep(0.3)
    return None


def collect_images(page, links, extra):
    srcs = []
    if not extra:
        # Всички снимки се виждат в поста - отваряме всяка поотделно.
        for href in links:
            page.goto(href, wait_until="domcontentloaded")
            src = main_image_src(page, srcs[-1] if srcs else None)
            if src:
                srcs.append(src)
        return srcs

    # Има "+N" - отваряме първата и вървим напред със стрелката.
    expected = len(links) + extra
    page.goto(links[0], wait_until="domcontentloaded")
    seen = set()
    while len(srcs) < expected:
        src = main_image_src(page, srcs[-1] if srcs else None)
        key = photo_key(page.url)
        if not src or key in seen:
            break
        seen.add(key)
        srcs.append(src)
        old = page.url
        page.keyboard.press("ArrowRight")
        try:
            page.wait_for_url(lambda u: u != old, timeout=8000)
        except PWTimeout:
            break
    return srcs


def download(ctx, url, path_no_ext):
    r = ctx.request.get(url, timeout=60000)
    if not r.ok:
        return None
    ctype = r.headers.get("content-type", "")
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(ctype.split(";")[0], ".jpg")
    path = path_no_ext.with_suffix(ext)
    path.write_bytes(r.body())
    return path


def scrape(ctx, url):
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    if "/login" in page.url or "checkpoint" in page.url:
        page.close()
        raise RuntimeError("Facebook поиска вход. Пусни пак: python fb_post.py --login")

    try:
        page.wait_for_selector(
            '[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], '
            f'[data-ad-preview="message"], {MAIN_IMAGE}',
            timeout=15000,
        )
    except PWTimeout:
        pass  # пост само със снимка/без текст - продължаваме
    time.sleep(1.5)

    if page.evaluate(SEE_MORE_JS):
        time.sleep(1)
    data = page.evaluate(EXTRACT_JS)
    final_url = page.url

    if urlparse(final_url).path.startswith(("/photo", "/photo.php")) or "/photos/" in final_url:
        # Линкът е директно към снимка.
        src = main_image_src(page)
        images = [src] if src else []
    else:
        images = collect_images(page, data["links"], data["extra"])
    page.close()

    folder = OUT_DIR / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{post_id(final_url)[:40]}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "text.txt").write_text(data["text"], encoding="utf-8")

    saved = []
    for i, src in enumerate(images, 1):
        p = download(ctx, src, folder / f"{i:02d}")
        if p:
            saved.append(p.name)

    (folder / "info.json").write_text(
        json.dumps({"url": url, "final_url": final_url, "text": data["text"],
                    "images": images, "files": saved}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return folder, data["text"], saved


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Сваля текста и снимките от пост във Facebook.")
    ap.add_argument("urls", nargs="*", help="линк(ове) към пост")
    ap.add_argument("-f", "--file", help="текстов файл с линкове, по един на ред")
    ap.add_argument("--login", action="store_true", help="отвори браузър, за да влезеш в акаунта си")
    ap.add_argument("--headless", action="store_true", help="не показвай прозореца на браузъра")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        urls += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]

    with sync_playwright() as p:
        if args.login:
            login(p)
            if not urls:
                return
        if not urls:
            ap.print_help()
            return

        browser = launch(p, headless=args.headless)
        ctx = new_context(browser)
        if not logged_in(ctx):
            browser.close()
            sys.exit("Не си влязъл във Facebook. Пусни първо: python fb_post.py --login")
        for url in urls:
            print(f"\n→ {url}")
            try:
                folder, text, saved = scrape(ctx, url)
            except Exception as e:
                print(f"  Грешка: {e}")
                continue
            preview = text[:120].replace("\n", " ") + ("…" if len(text) > 120 else "")
            print(f"  Текст: {preview or '(няма)'}")
            print(f"  Снимки: {len(saved)}  →  {folder}")
        ctx.storage_state(path=str(SESSION_FILE))  # обновени бисквитки за следващия път
        browser.close()


if __name__ == "__main__":
    main()
