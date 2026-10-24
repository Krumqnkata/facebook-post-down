"""
fb_post.py - сваля текста и снимките от пост във Facebook.

Първо пускане (влизаш в акаунта си само веднъж, сесията се пази във fb_session.json):
    python fb_post.py --login

След това:
    python fb_post.py "https://www.facebook.com/.../posts/..."
    python fb_post.py линк1 линк2 линк3
    python fb_post.py -f links.txt          (по един линк на ред)
    python fb_post.py --headless "линк"     (без да се показва прозорец)

Или по потребител/страница (последните N поста):
    python fb_post.py -u zuck --limit 20
    python fb_post.py -u "https://www.facebook.com/profile.php?id=100000000000000"

Резултатът е в папка downloads/<дата>_<id>/ : text.txt, 01.jpg, 02.jpg ..., info.json
(при -u: downloads/<потребител>/<дата>_<id>/)
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "fb_session.json"  # бисквитките за вход - не го давай на никого
OUT_DIR = HERE / "downloads"

MAIN_IMAGE = 'img[data-visualcompletion="media-vc-image"]'

# Facebook често сменя HTML-а си - ако текстът спре да се намира, тук се добавя нов селектор.
FIND_POST_JS = r"""
  const MSG = '[data-ad-rendering-role="story_message"], [data-ad-comet-preview="message"], [data-ad-preview="message"]';
  // Когато си влязъл, FB отваря поста в прозорец (role="dialog") върху началната страница,
  // а отдолу остават постове от стената - затова прозорецът е с предимство.
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(d => d.querySelector('[role="article"]'));
  const root = dialogs.length ? dialogs[dialogs.length - 1]
    : (document.querySelector('[role="main"]') || document.body);
  const msg = root.querySelector(MSG);
  const scope = (msg && msg.closest('[role="article"]')) || root.querySelector('[role="article"]') || root;
"""

EXTRACT_JS = "() => {" + FIND_POST_JS + r"""

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

SEE_MORE_JS = "() => {" + FIND_POST_JS + r"""
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


POST_LINK = re.compile(r"/posts/|/permalink\.php|story_fbid=|/videos/|/reel/")

POST_LINKS_JS = r"""
(unit) => [...unit.querySelectorAll('a[href]')]
  .map(a => a.href)
  .filter(h => !/[?&]comment_id=/.test(h))
"""


def profile_url(user):
    if user.startswith("http"):
        return user
    if user.isdigit():
        return f"https://www.facebook.com/profile.php?id={user}"
    return f"https://www.facebook.com/{user.lstrip('@')}"


def user_folder(user):
    if user.startswith("http"):
        u = urlparse(user)
        q = parse_qs(u.query)
        user = q["id"][0] if "id" in q else u.path.strip("/").split("/")[0]
    return re.sub(r"[^\w.-]", "_", user.lstrip("@")) or "user"


def clean_post_url(href):
    u = urlparse(href)
    q = parse_qs(u.query)
    keep = {k: q[k][0] for k in ("story_fbid", "id", "v") if k in q}
    return u._replace(query=urlencode(keep), fragment="").geturl()


def unit_post_link(unit):
    """Връща линка към поста от един елемент на стената."""
    for attempt in range(2):
        for href in unit.evaluate(POST_LINKS_JS):
            if POST_LINK.search(href):
                return clean_post_url(href)
        if attempt == 0:
            # Когато си влязъл, FB слага истинския линк на датата чак при посочване с мишката.
            stubs = unit.locator('a[href="#"], a[role="link"]:not([href])')
            for i in range(min(stubs.count(), 6)):
                try:
                    stubs.nth(i).hover(timeout=2000)
                except PWError:
                    pass
            time.sleep(0.5)
    return None


def find_user_posts(ctx, user, limit):
    """Отваря профила/страницата, скролира и събира линковете на последните `limit` поста."""
    page = ctx.new_page()
    page.goto(profile_url(user), wait_until="domcontentloaded")
    if "/login" in page.url or "checkpoint" in page.url:
        page.close()
        raise RuntimeError("Facebook поиска вход. Пусни пак: python fb_post.py --login")
    time.sleep(3)

    urls, done, idle = [], set(), 0
    while len(urls) < limit and idle < 6:
        before = len(urls)
        for pos in page.evaluate("() => [...document.querySelectorAll('[aria-posinset]')].map(e => e.getAttribute('aria-posinset'))"):
            if pos in done or len(urls) >= limit:
                continue
            unit = page.locator(f'[aria-posinset="{pos}"]').first
            try:
                unit.scroll_into_view_if_needed(timeout=3000)
                href = unit_post_link(unit)
            except PWError:
                continue
            if href is None:
                continue  # още не е заредено - пак ще го видим след скрола
            done.add(pos)
            if href not in urls:
                urls.append(href)
                print(f"  намерен пост {len(urls)}: {href}")
        idle = idle + 1 if len(urls) == before else 0
        page.mouse.wheel(0, 2500)
        time.sleep(1.5)
    page.close()
    return urls


def scrape(ctx, url, out_dir=OUT_DIR):
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

    folder = out_dir / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{post_id(final_url)[:40]}"
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


def ask_user():
    print("=== Сваляне на постове от Facebook ===\n")
    users = input("Потребител (име, id или линк; няколко - със запетая): ").strip()
    limit = input("Колко поста назад? [10]: ").strip()
    users = [u for u in re.split(r"[,\s]+", users) if u]
    return users, int(limit) if limit.isdigit() and int(limit) > 0 else 10


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Сваля текста и снимките от пост във Facebook.")
    ap.add_argument("urls", nargs="*", help="линк(ове) към пост")
    ap.add_argument("-f", "--file", help="текстов файл с линкове, по един на ред")
    ap.add_argument("-u", "--user", action="append", default=[],
                    help="потребител/страница (име, id или линк към профила); може няколко пъти")
    ap.add_argument("--limit", type=int, default=10, help="колко от последните поста да се свалят при -u (10)")
    ap.add_argument("--login", action="store_true", help="отвори браузър, за да влезеш в акаунта си")
    ap.add_argument("--headless", action="store_true", help="не показвай прозореца на браузъра")
    args = ap.parse_args()

    jobs = [(url, OUT_DIR) for url in args.urls]
    if args.file:
        jobs += [(l.strip(), OUT_DIR) for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]

    # Без аргументи (напр. от start.bat) - питаме кой потребител и колко поста.
    interactive = not jobs and not args.user and not args.login
    if interactive:
        args.user, args.limit = ask_user()
        args.headless = True
        if not args.user:
            return

    with sync_playwright() as p:
        if args.login:
            login(p)
            if not jobs and not args.user:
                return

        browser = launch(p, headless=args.headless)
        ctx = new_context(browser)
        if not logged_in(ctx):
            browser.close()
            if not interactive:
                sys.exit("Не си влязъл във Facebook. Пусни първо: python fb_post.py --login")
            print("\nПърво трябва да влезеш във Facebook (само веднъж).")
            login(p)
            browser = launch(p, headless=args.headless)
            ctx = new_context(browser)

        for user in args.user:
            print(f"\nТърся постове на {user} ...")
            try:
                found = find_user_posts(ctx, user, args.limit)
            except Exception as e:
                print(f"  Грешка: {e}")
                continue
            jobs += [(url, OUT_DIR / user_folder(user)) for url in found]

        for url, out_dir in jobs:
            print(f"\n→ {url}")
            try:
                folder, text, saved = scrape(ctx, url, out_dir)
            except Exception as e:
                print(f"  Грешка: {e}")
                continue
            preview = text[:120].replace("\n", " ") + ("…" if len(text) > 120 else "")
            print(f"  Текст: {preview or '(няма)'}")
            print(f"  Снимки: {len(saved)}  →  {folder}")
        ctx.storage_state(path=str(SESSION_FILE))  # обновени бисквитки за следващия път
        browser.close()

    if interactive:
        print(f"\nГотово. Свалените постове са в {OUT_DIR}")
        if OUT_DIR.exists():
            os.startfile(OUT_DIR)


if __name__ == "__main__":
    main()
