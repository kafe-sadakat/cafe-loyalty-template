/**
 * Tailwind (v3.4.17) derleme yapılandırması — tools/build_css.py ile kullanılır.
 * Renkler CSS değişkenlerine (RGB üçlüleri) bağlıdır; paletler tailwind.input.css içinde tanımlıdır:
 *   :root                    → müşteri uygulaması (orijinal zümrüt/krem palet)
 *   html[data-theme=soft]    → yönetim panelleri, Soft Light (krem/şampanya)
 *   html[data-theme=dark]    → yönetim panelleri, Dark (derin kahve + altın)
 */
const c = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;

module.exports = {
  darkMode: "class",
  content: ["../app/templates/**/*.html", "../app/static/js/panel.js"],
  plugins: [require("@tailwindcss/forms"), require("@tailwindcss/container-queries")],
  theme: {
    extend: {
      colors: {
        primary: c("primary"),
        "primary-container": c("primary-container"),
        "on-primary": c("on-primary"),
        "on-primary-container": c("on-primary-container"),
        "primary-fixed": c("primary-fixed"),
        "primary-fixed-dim": c("primary-fixed-dim"),
        "on-primary-fixed": c("on-primary-fixed"),
        "on-primary-fixed-variant": c("on-primary-fixed-variant"),
        secondary: c("secondary"),
        "secondary-container": c("secondary-container"),
        "on-secondary-container": c("on-secondary-container"),
        "secondary-fixed": c("secondary-fixed"),
        "secondary-fixed-dim": c("secondary-fixed-dim"),
        "on-secondary-fixed": c("on-secondary-fixed"),
        "on-secondary-fixed-variant": c("on-secondary-fixed-variant"),
        tertiary: c("tertiary"),
        "tertiary-container": c("tertiary-container"),
        "on-secondary": c("on-secondary"),
        "on-tertiary": c("on-tertiary"),
        "on-error": c("on-error"),
        "on-tertiary-container": c("on-tertiary-container"),
        brass: "#C9A66B",
        surface: c("surface"),
        "surface-low": c("surface-low"),
        "surface-dim": c("surface-dim"),
        "surface-bright": c("surface-bright"),
        "surface-container": c("surface-container"),
        "surface-container-lowest": c("surface-container-lowest"),
        "surface-container-low": c("surface-container-low"),
        "surface-container-high": c("surface-container-high"),
        "surface-container-highest": c("surface-container-highest"),
        "surface-variant": c("surface-variant"),
        "surface-high": c("surface-high"),
        "surface-highest": c("surface-highest"),
        "on-surface": c("on-surface"),
        "on-surface-variant": c("on-surface-variant"),
        background: c("background"),
        "on-background": c("on-background"),
        "inverse-surface": c("inverse-surface"),
        "inverse-on-surface": c("inverse-on-surface"),
        "inverse-primary": c("inverse-primary"),
        "surface-tint": c("surface-tint"),
        outline: c("outline"),
        "outline-variant": c("outline-variant"),
        error: c("error"),
        "error-container": c("error-container"),
        "on-error-container": c("on-error-container"),
      },
      fontFamily: {
        headline: ["Newsreader", "serif"],
        body: ["Plus Jakarta Sans", "sans-serif"],
        label: ["Plus Jakarta Sans", "sans-serif"],
      },
    },
  },
};
