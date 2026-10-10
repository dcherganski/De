# KATE — сайт на проекта

Статичен, mobile-first сайт на английски за представяне на KATE пред инвеститори.
Без framework, без build стъпка, без бисквитки и без външни заявки (шрифтове, аналитика).

## Файлове

| Файл | Какво е |
|---|---|
| `index.html` | Основната страница: Hero, Vision, Capabilities, Workspace (концепция), Privacy & Memory, Investors, About/Contact |
| `privacy.html` | Проектна бележка за поверителност (чернова, **не е преглеждана от юрист**) |
| `styles.css` | Целият дизайн. Цветовете са променливи в `:root` в началото на файла |
| `script.js` | Мобилно меню, бутон „Copy address“, година във футъра |
| `404.html` | Страница „не е намерено“ |
| `favicon.svg`, `apple-touch-icon.png`, `og-image.png` | Иконки и картинка за споделяне в социални мрежи |
| `robots.txt` | Разрешава индексиране |

Контактът е чрез `mailto:` — няма форма, затова няма нужда от сървър и защита от спам.

## Стартиране локално

```bash
cd kate-site
python3 -m http.server 8000
```

Отвори http://localhost:8000. От телефон в същата Wi-Fi мрежа: `http://<IP-на-компютъра>:8000`
(IP се вижда с `ipconfig` на Windows или `ip addr` на Linux/macOS).
Може и просто да отвориш `index.html` с двоен клик.

## Публикуване (безплатно)

> Преди публикуване прегледай текста. Сайтът става публичен за всички.

### Вариант А: Cloudflare Pages (препоръчан, работи и от телефон)

1. Регистрация на https://dash.cloudflare.com (безплатно).
2. **Workers & Pages → Create → Pages → Upload assets**.
3. Име на проекта, напр. `kate-ai` → качи съдържанието на папка `kate-site` (може като `.zip`).
4. Сайтът е на `https://kate-ai.pages.dev`. Собствен домейн: **Custom domains** в проекта.

С команден ред:

```bash
npx wrangler login
npx wrangler pages deploy kate-site --project-name kate-ai
```

### Вариант Б: GitHub Pages

1. В GitHub: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. **Actions → „KATE site: deploy to GitHub Pages“ → Run workflow** (клон `main`).
3. Адресът се показва в стъпката `deploy`, обикновено `https://<потребител>.github.io/<repo>/`.

Workflow-ът се стартира **само ръчно**, за да не стане нищо публично случайно.
Ако хранилището е частно, GitHub Pages изисква платен план — тогава ползвай Cloudflare.

## След като има домейн

- Смени `og:image` в `index.html` на пълен адрес (`https://домейн/og-image.png`).
- Добави `<link rel="canonical" href="https://домейн/">` и `sitemap.xml`.

## Какво остава

- Преглед и одобрение на текста от основателя.
- Юридически преглед на политиката за паметта и поверителността преди реални регистрации.
- Домейн и публикуване.
- Ако по-късно трябва контактна форма: реална обработка (напр. Cloudflare Pages Functions + Turnstile срещу спам).
- Бъдещата AI платформа (акаунти, памет, backend) ще е отделно приложение; сайтът е само представяне.
