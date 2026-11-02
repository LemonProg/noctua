# Prompt: build the brand guide from your website

Instead of filling in `charte.md` by hand, let Claude Code read your company's website and write it for you:
identity, offers, tone, colours, typography and visual style.

## How to use it

1. Open a terminal at the root of the project and start Claude Code: `claude`.
2. Copy the prompt below, replace `https://www.example.com` with your website, and paste it.
3. Review `charte.md` (5 minutes). Fix anything marked as to confirm, and complete any `[À REMPLIR]` marker left.
4. Run `agence init` to check the guide.

Then, for the **art direction**: drop the images Claude saved in `references-site/` into the interface
(Réglages > Références, several at once) and click "Analyser mes références". The visual agent turns them into an art direction for you to approve.

---

## The prompt

````text
Read my company's website and write the brand guide `charte.md` for the agency in this repository.

Website: https://www.example.com

Steps:

1. Explore the site: home page, about, services/offers/pricing, portfolio or case studies, contact, legal
   notice, and the links to social networks. Read up to about 15 pages, the most informative first.

2. Extract the visual identity from the code, not from guesses:
   - download the HTML and the CSS files with curl;
   - list the colours actually used (hex codes), ranked by how often and where they appear
     (background, text, accent, buttons);
   - list the font families (font-family, @font-face, Google Fonts or Adobe Fonts links), noting
     which are for headings and which for body text, and their licence when you can tell
     (Google Fonts = free; otherwise "licence to check");
   - describe the visual style from the page structure and images: layout, spacing, photo style,
     shapes, density, light or dark.

3. Save 6 to 10 representative images from the site to `references-site/` (hero images, project
   visuals, social media visuals, the logo). Only images, max 2000 px wide, JPEG or PNG.
   If you find the logo as an SVG or PNG, also copy it to `app/marque/logo.svg` (or `.png`).
   Do not download any font file.

4. Write `charte.md` following the exact structure and headings of `charte.exemple.md`.
   - Write the guide in the language of the website.
   - Only write what the site actually says or shows. Quote exact wording for offers, prices
     (keep "from"/"à partir de" if the site says it), lead times and promises.
   - Mark anything you deduced rather than read with "(to confirm)", in the site's language.
   - If a piece of information cannot be found (for example banned words), leave the
     `[À REMPLIR]` marker on that line so I complete it myself. Never invent a client,
     a figure, a price or a result.
   - In "Examples of good content", quote 2 or 3 short passages from the site that best show
     the tone, each as a quote.
   - Add a "Source" line under the title: the site and today's date.

5. Finish with a short summary: what you filled in, what is marked "to confirm", which
   `[À REMPLIR]` markers remain, the fonts I need to install in `app/polices-marque/`
   (with where to get them), and the files saved in `references-site/`.

Do not modify any other file. Do not commit anything.
````

## Notes

- `charte.md`, `references-site/` and your fonts are ignored by git: your brand never ends up in the repository.
- The agents only use what is written in the guide. Spend the 5 minutes reviewing it: a wrong price or promise
  there would end up in your posts.
- Run it again whenever your website changes.
