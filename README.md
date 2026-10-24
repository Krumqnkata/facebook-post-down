# facebook-post-down

Сваля текста и снимките (в пълен размер) от пост във Facebook.

## Инсталация

```bash
pip install -r requirements.txt
```

Ползва вече инсталиран Microsoft Edge или Google Chrome. Ако нямаш нито един от двата:

```bash
python -m playwright install chromium
```

## Употреба

Първо влизаш в акаунта си (само веднъж). Сесията се пази във `fb_session.json`:

```bash
python fb_post.py --login
```

След това:

```bash
python fb_post.py "https://www.facebook.com/.../posts/..."
python fb_post.py линк1 линк2 линк3
python fb_post.py -f links.txt          # по един линк на ред
python fb_post.py --headless "линк"     # без прозорец на браузъра
```

### По потребител или страница

Вместо линкове може да дадеш потребителско име, id или линк към профила. Скриптът отваря профила, скролира стената и сваля последните `--limit` поста (по подразбиране 10):

```bash
python fb_post.py -u zuck --limit 20
python fb_post.py -u 100000000000000                 # числово id
python fb_post.py -u "https://www.facebook.com/nasa"
python fb_post.py -u nasa -u spacex --limit 5        # няколко наведнъж
```

Постовете отиват в `downloads/<потребител>/`.

## Резултат

За всеки пост се прави папка `downloads/<дата>_<id>/` с:

- `text.txt` - текстът на поста (с натиснато „Виж още“)
- `01.jpg`, `02.jpg`, … - снимките в оригинален размер
- `info.json` - линк, текст и адресите на снимките

## Ограничения

- Facebook често сменя HTML-а си. Ако текстът спре да се намира, обнови селекторите в `EXTRACT_JS`.
- Видеата не се свалят.
- Снимките от коментарите не се свалят.
- `fb_session.json` съдържа бисквитките за вход в акаунта ти. Не го споделяй (вече е в `.gitignore`).
