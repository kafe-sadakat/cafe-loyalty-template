/* Müdür ve Çalışan panelleri için ortak yardımcılar (harici kütüphane yok). */
(function () {
  const APP_BASE = (location.pathname === "/k" || location.pathname.startsWith("/k/")) ? "/k/" : "/";
  window.APP_BASE = APP_BASE;

  window.esc = function (v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  window.imgSrc = function (u) {
    if (!u) return "";
    return APP_BASE + String(u).replace(/^\/+/, "").replace(/^k\//, "");
  };

  window.fmtDate = function (iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    return d.toLocaleString("tr-TR", { timeZone: "Europe/Istanbul", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  window.fmtPrice = function (n) {
    return Number(n || 0).toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " ₺";
  };

  window.toast = function (msg, type) {
    let el = document.getElementById("pn-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "pn-toast";
      el.className = "fixed top-4 left-1/2 -translate-x-1/2 z-[200] max-w-sm w-[92%] rounded-2xl px-4 py-3 shadow-xl text-sm font-semibold";
      // Bildirime dokununca hemen kapanır (süresinin dolması beklenmez); z-index derlenmemiş sınıfa bağlı kalmaz.
      el.style.zIndex = "100001";
      el.style.cursor = "pointer";
      el.title = "Kapatmak için dokunun";
      el.addEventListener("click", () => { clearTimeout(window._pnToast); el.classList.add("hidden"); });
      document.body.appendChild(el);
    }
    el.className = "fixed top-4 left-1/2 -translate-x-1/2 z-[200] max-w-sm w-[92%] rounded-2xl px-4 py-3 shadow-xl text-sm font-semibold " +
      (type === "error" ? "bg-error-container text-error" : "emerald-pass-gradient text-white");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(window._pnToast);
    window._pnToast = setTimeout(() => el.classList.add("hidden"), 4200);
  };

  /** JSON API çağrısı. 401 → onUnauthorized (giriş formu). */
  window.api = async function (path, opts) {
    opts = opts || {};
    const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (opts.body instanceof FormData) {
      init.body = opts.body;
    } else if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(APP_BASE + path.replace(/^\//, ""), init);
    if (res.status === 401 && window.onUnauthorized) window.onUnauthorized();
    let data = {};
    try { data = await res.json(); } catch (e) { data = { status: "error", message: "Sunucu yanıtı okunamadı." }; }
    if (res.status === 401) data.status = "error";
    return data;
  };

  // ---------- Tema: Soft Light (krem) / Dark (kahve + altın) ----------
  const THEME_KEY = "bb-theme";
  window.getTheme = function () {
    try { return localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "soft"; } catch (e) { return "soft"; }
  };
  window.applyTheme = function (t) {
    document.documentElement.setAttribute("data-theme", t);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", t === "dark" ? "#12100E" : "#FAF9F7");
    document.querySelectorAll("[data-theme-icon]").forEach((el) => { el.textContent = t === "dark" ? "light_mode" : "dark_mode"; });
    // Lucide satır içi ikon kullanan düğmeler: gün/ay SVG çiftini gizle-göster
    document.querySelectorAll("[data-theme-ico-swap]").forEach((wrap) => {
      const sun = wrap.querySelector("[data-ico-sun]");
      const moon = wrap.querySelector("[data-ico-moon]");
      if (sun) sun.classList.toggle("hidden", t !== "dark");
      if (moon) moon.classList.toggle("hidden", t === "dark");
    });
    document.querySelectorAll("[data-theme-toggle]").forEach((el) => {
      el.setAttribute("aria-label", t === "dark" ? "Soft Light moda geç" : "Dark moda geç");
      el.setAttribute("title", t === "dark" ? "Soft Light moda geç" : "Dark moda geç");
    });
  };
  window.toggleTheme = function () {
    const next = window.getTheme() === "dark" ? "soft" : "dark";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    window.applyTheme(next);
  };
  document.addEventListener("DOMContentLoaded", () => window.applyTheme(window.getTheme()));

  // ---------- Telefon girişi: 5XX XXX XX XX (başında 0 yok) ----------
  window.formatPhoneInput = function (value) {
    let d = String(value || "").replace(/\D/g, "");
    if (d.length > 10 && d.startsWith("90")) d = d.slice(2);
    d = d.replace(/^0+/, "").slice(0, 10);
    const parts = [d.slice(0, 3), d.slice(3, 6), d.slice(6, 8), d.slice(8, 10)].filter(Boolean);
    return parts.join(" ");
  };
  window.bindPhoneMask = function (el) {
    if (!el) return;
    el.setAttribute("maxlength", "14");
    el.setAttribute("placeholder", "5XX XXX XX XX");
    el.addEventListener("input", () => { el.value = window.formatPhoneInput(el.value); });
  };

  // ---------- Uygulama olarak yükleme (ADR-43): Yönetim / Kasa paneli ana ekrana eklenir ----------
  // Düğme ([data-pwa-install]) yalnızca yüklenebilirken görünür: Chrome/Edge/Samsung yükleme penceresi ya da iPhone/iPad
  // (Safari'de "Ana Ekrana Ekle" talimatı). Uygulama içinden açıldıysa gizli kalır.
  const hasManifest = () => !!document.querySelector('link[rel="manifest"]');
  const isStandalone = () => {
    try { return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true; } catch (e) { return false; }
  };
  const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  function paintInstall() {
    const show = hasManifest() && !isStandalone() && (!!window.__panelInstallEvt || isIos());
    document.querySelectorAll("[data-pwa-install]").forEach((el) => { el.style.display = show ? "" : "none"; });
  }
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); window.__panelInstallEvt = e; paintInstall(); });
  window.addEventListener("panel-installable", paintInstall);
  window.addEventListener("appinstalled", () => {
    window.__panelInstallEvt = null;
    paintInstall();
    window.toast("Uygulama yüklendi. Ana ekrandan açabilirsiniz.");
  });
  window.panelInstall = async function () {
    const evt = window.__panelInstallEvt;
    if (evt) {
      evt.prompt();
      try { await evt.userChoice; } catch (e) {}
      window.__panelInstallEvt = null;
      paintInstall();
      return;
    }
    if (isIos()) {
      const wrap = document.createElement("div");
      wrap.className = "fixed inset-0 bg-black/50 flex items-center justify-center p-5";
      wrap.style.zIndex = "300";
      wrap.innerHTML =
        '<div class="bg-surface rounded-3xl max-w-sm w-full p-6 shadow-2xl space-y-3">' +
        '<p class="text-sm text-on-surface font-semibold">Ana ekrana ekleyin</p>' +
        '<p class="text-sm text-on-surface-variant">1. Safari’de alttaki <strong>Paylaş</strong> düğmesine dokunun.</p>' +
        '<p class="text-sm text-on-surface-variant">2. <strong>Ana Ekrana Ekle</strong>’yi seçin.</p>' +
        '<p class="text-sm text-on-surface-variant">Uygulamayı ilk açışta bir kez giriş yapmanız gerekir.</p>' +
        '<div class="flex justify-end"><button type="button" class="pn-btn">Tamam</button></div></div>';
      wrap.addEventListener("click", (e) => { if (e.target === wrap || e.target.closest("button")) wrap.remove(); });
      document.body.appendChild(wrap);
    }
  };
  document.addEventListener("DOMContentLoaded", paintInstall);
  // Müşteri kartıyla aynı servis çalışanı (aynı adres + kapsam → tek kayıt; müşteri bildirim aboneliği bozulmaz)
  try {
    if (hasManifest() && "serviceWorker" in navigator && window.isSecureContext !== false) {
      navigator.serviceWorker.register(APP_BASE + "sw.js").catch(() => {});
    }
  } catch (e) {}

  window.$ = (sel, root) => (root || document).querySelector(sel);
  window.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  window.logout = async function () {
    await api("panel/logout", { method: "POST" });
    location.reload();
  };

  /** Metin girişi penceresi (tarayıcı prompt() yerine; iptalde null döner). */
  window.promptBox = function (message, initial) {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "fixed inset-0 z-[300] bg-black/50 flex items-center justify-center p-5";
      wrap.innerHTML =
        '<form class="bg-surface rounded-3xl max-w-sm w-full p-6 shadow-2xl space-y-4">' +
        '<p class="text-sm text-on-surface">' + esc(message) + "</p>" +
        '<input class="pn-input" type="text" value="' + esc(initial || "") + '" />' +
        '<div class="flex gap-2 justify-end"><button type="button" data-r="0" class="pn-btn pn-btn-ghost">Vazgeç</button>' +
        '<button type="submit" class="pn-btn">Tamam</button></div></form>';
      const input = wrap.querySelector("input");
      wrap.addEventListener("click", (e) => { if (e.target.closest('button[data-r="0"]')) { wrap.remove(); resolve(null); } });
      wrap.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); const v = input.value.trim(); wrap.remove(); resolve(v || null); });
      document.body.appendChild(wrap);
      input.focus();
    });
  };

  /** Onay penceresi (tarayıcı confirm() yerine). */
  window.confirmBox = function (message, okLabel) {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "fixed inset-0 z-[300] bg-black/50 flex items-center justify-center p-5";
      wrap.innerHTML =
        '<div class="bg-surface rounded-3xl max-w-sm w-full p-6 shadow-2xl space-y-4">' +
        '<p class="text-sm text-on-surface">' + esc(message) + "</p>" +
        '<div class="flex gap-2 justify-end">' +
        '<button type="button" data-r="0" class="pn-btn pn-btn-ghost">Vazgeç</button>' +
        '<button type="button" data-r="1" class="pn-btn pn-btn-danger">' + esc(okLabel || "Evet") + "</button></div></div>";
      wrap.addEventListener("click", (e) => {
        const b = e.target.closest("button[data-r]");
        if (!b) return;
        wrap.remove();
        resolve(b.dataset.r === "1");
      });
      document.body.appendChild(wrap);
    });
  };

  // ---------- Kasa "Kazanılan Tek Seferlik Bonuslar (Kanıt Tablosu)" ----------
  // Müşteri JSON'unda: welcome/pwa/push_bonus_status (received | blocked | none), *_bonus_at, recent_stamps_log.
  window.fmtIsoParts = function (iso) {
    const s = String(iso || "");
    if (s.length < 16) return { d: "—", t: "—" };
    return { d: s.slice(8, 10) + "." + s.slice(5, 7) + "." + s.slice(0, 4), t: s.slice(11, 16) };
  };
  const PROOF_ROWS = [
    { key: "welcome", icon: "🎉", title: "İlk Kayıt (Hoş Geldin) Damgası", none: "⏳ Henüz Alınmadı", blocked: "🚫 HAKKI YOK (Daha Önce Hesap Sıfırlamış)" },
    { key: "pwa", icon: "📲", title: "Uygulama İndirme (PWA) Damgası", none: "⏳ Henüz İndirmedi", blocked: "🚫 BU CİHAZDA DAHA ÖNCE KULLANILDI" },
    { key: "push", icon: "🔔", title: "Bildirim Açma Damgası", none: "⏳ Bildirimi Açık Değil", blocked: "🚫 BU CİHAZDA DAHA ÖNCE KULLANILDI" }
  ];
  window.bonusProofHtml = function (c) {
    const boxes = PROOF_ROWS.map((p) => {
      const st = c[p.key + "_bonus_status"] || "none";
      let cls = "bb-proof-wait", text = p.none;
      if (st === "received") {
        const f = window.fmtIsoParts(c[p.key + "_bonus_at"]);
        cls = "bb-proof-ok";
        text = "✅ ALINDI (Tarih: " + f.d + " - Saat: " + f.t + ")";
      } else if (st === "blocked") {
        cls = "bb-proof-no";
        text = p.blocked;
      }
      return '<div class="bb-proof ' + cls + '"><div class="bb-proof-title">' + p.icon + " " + esc(p.title) + '</div><div class="bb-proof-text">' + esc(text) + "</div></div>";
    }).join("");
    return '<div class="bb-group"><div class="bb-label" style="margin:0">🎁 Kazanılan Bonus Hakları</div>' +
      window.bonusHistoryBadges(c, false) +
      '<div class="bb-proof-grid">' + boxes + "</div></div>" +
      '<div class="bb-group"><div class="bb-label" style="margin:0">⚡ Müşterinin Anlık Cihaz Durumu</div>' +
      window.liveStatusBadges(c) + window.liveStatusHint(c) + "</div>";
  };

  // ---------- İki ayrı gösterge grubu ----------
  // Grup 1: bonus_history {welcome,pwa,push,campaign: claimed|used|none} — hak kullanım geçmişi (uygulama silinse de değişmez).
  // Grup 2: current_status {push_active,is_pwa,account_recreated} — şu anki cihaz/izin durumu.
  const pill = (cls, text, tip) =>
    '<span class="bb-pill bb-pill-' + cls + '"' + (tip ? ' title="' + esc(tip) + '"' : "") + ">" + esc(text) + "</span>";
  const HISTORY_LABEL = { claimed: ["ok", "Alındı"], used: ["used", "Kullanıldı"], none: ["none", "Yok"] };
  window.bonusHistoryBadges = function (c, short) {
    const h = c.bonus_history || {};
    const word = short ? "" : " Bonusu";
    const items = [["welcome", "Kayıt" + word], ["pwa", "İndirme" + word], ["push", "Bildirim" + word]];
    const out = items.map(([key, label]) => {
      const v = HISTORY_LABEL[h[key]] || HISTORY_LABEL.none;
      return pill(v[0], label + ": " + v[1], v[0] === "used" ? "Hak daha önce kullanılmış (cihaz/numara) — tekrar verilmez" : "");
    });
    // Kısa kipte (müdür listesi) kampanya ödülü ayrı "Kampanya" satırında gösterilir: üç sabit sütun bozulmaz
    if (!short && h.campaign === "claimed") out.push(pill("camp", "Kampanya: Alındı"));
    return '<div class="bb-pill-row">' + out.join("") + "</div>";
  };
  // short: müdür listesindeki sabit sütunlu şerit — aynı bilgi, simgesiz ve kısa (durumu renk anlatır, ayrıntı ipucunda)
  window.liveStatusBadges = function (c, short) {
    const s = c.current_status || {};
    const t = (full, brief) => (short ? brief : full);
    const out = [
      s.push_active ? pill("ok", t("Bildirim: Açık 🟢", "Bildirim: Açık"))
        : s.push_no_consent ? pill("used", t("Bildirim: İzinli, onay yok 🟡", "Bildirim: Onay yok"), "Tarayıcı izni var ama kampanya (ticari ileti) onayı yok — kampanya bildirimi gitmez")
        : pill("none", t("Bildirim: Kapalı ⚪", "Bildirim: Kapalı")),
      s.is_pwa ? pill("ok", t("Cihaz: Yüklü PWA 📱", "Cihaz: Uygulama"), t("", "Uygulama ana ekrana yüklü (PWA)"))
        : pill("none", t("Cihaz: Tarayıcı (Web) 🌐", "Cihaz: Tarayıcı"), t("", "Uygulama yüklü değil; tarayıcıdan kullanıyor")),
      s.account_recreated ? pill("used", t("Hesap: Yeniden Kayıt ⚠️", "Hesap: Yeniden Kayıt"), t("", "Hesabını silip yeniden kayıt olmuş"))
        : pill("none", "Hesap: Normal")
    ];
    return '<div class="bb-pill-row">' + out.join("") + "</div>";
  };
  window.liveStatusHint = function (c) {
    const s = c.current_status || {};
    if (s.is_pwa && s.push_active) return "";
    const h = c.bonus_history || {};
    const open = [];
    if (!s.is_pwa && h.pwa === "none") open.push("uygulamayı yüklerse");
    if (!s.push_active && h.push === "none") open.push("bildirimleri açarsa");
    if (!open.length) return "";
    return '<div class="text-xs text-on-surface-variant">💬 Müşteri ' + open.join(" ve ") + " hediye damga kazanır.</div>";
  };
  window.proofNeedsNote = function (c) {
    return PROOF_ROWS.some((p) => (c[p.key + "_bonus_status"] || "none") !== "none");
  };
  window.proofNoteStrip = function (c) {
    if (!window.proofNeedsNote(c)) return "";
    return '<div class="bb-note-strip" role="note">💡 KASİYER NOTU: Müşteri \u201Cuygulamayı indirdim / bildirimi açtım ama damgam gelmedi\u201D derse yukarıdaki yeşil kutularda yazan tarih ve saati müşteriye gösteriniz. Sistem hediye damgasını o tarihte otomatik eklemiştir; kesinlikle ekstra manuel damga basmayınız!</div>';
  };
  window.recentStampsHtml = function (c) {
    const list = c.recent_stamps_log || [];
    const rows = list.length
      ? list.map((s) => {
          const f = window.fmtIsoParts(s.at);
          return '<div class="bb-recent-row"><span class="bb-chip">' + esc(s.label) + '</span><span class="font-mono text-xs">' + esc(f.d + " " + f.t) + "</span>" +
            (s.actor ? '<span class="text-xs text-outline">' + esc(s.actor) + "</span>" : "") + "</div>";
        }).join("")
      : '<div class="text-sm text-on-surface-variant">Henüz damga yok.</div>';
    return '<div class="flex flex-col gap-1"><div class="bb-label" style="margin:0">Son 5 İşlem Özeti</div>' + rows + "</div>";
  };

  // ---------- Ödüllü kampanyalar (ADR-26) ----------
  // Müşteri JSON'unda `campaigns`: [{campaign_id, title, reward_stamps, active, status, proof_name, reviewed_at, reviewed_by, reject_reason}]
  // status: none | joined | pending | approved | rejected. Damga YALNIZCA onayla verilir.
  const CAMP_STATUS = {
    none: ["none", "Katılmadı"],
    joined: ["camp", "Katıldı · henüz bildirmedi"],
    pending: ["used", "⏳ Doğrulama bekliyor"],
    approved: ["ok", "✅ Damga verildi"],
    rejected: ["none", "✖ Reddedildi"]
  };
  const shortText = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  window.campaignStatusPill = function (status) {
    const v = CAMP_STATUS[status] || CAMP_STATUS.none;
    return pill(v[0], v[1]);
  };
  /** Müdür listesi: yalnızca katıldığı kampanyalar, kampanya adıyla tek satır rozet. */
  window.campaignMiniBadges = function (c) {
    const items = (c.campaigns || []).filter((x) => x.status !== "none");
    const pills = items.map((x) => {
      const v = CAMP_STATUS[x.status] || CAMP_STATUS.none;
      return pill(v[0], "🎯 " + shortText(x.title, 28) + ": " + v[1], x.title);
    });
    // Kampanya damgası almış ama listede onaylı kampanyası görünmeyen (ör. kampanyası silinmiş) müşteri
    if ((c.bonus_history || {}).campaign === "claimed" && !items.some((x) => x.status === "approved")) {
      pills.push(pill("camp", "Kampanya damgası alındı"));
    }
    if (!pills.length) return "";
    return '<div class="bb-pill-row">' + pills.join("") + "</div>";
  };
  /** Kasa / müdür müşteri kartı: aktif ödüllü kampanyalar + katılım durumu + onay/ret düğmeleri. */
  window.campaignBoxHtml = function (c) {
    const items = c.campaigns || [];
    if (!items.length) return "";
    const rows = items.map((x) => {
      const info = [];
      if (x.proof_name) info.push("Yorumdaki adı: <strong>" + esc(x.proof_name) + "</strong>");
      if (x.status === "approved" && x.reviewed_at) {
        const f = window.fmtIsoParts(x.reviewed_at);
        info.push("Onay: " + esc(f.d + " " + f.t) + (x.reviewed_by ? " (" + esc(x.reviewed_by) + ")" : ""));
      }
      if (x.status === "rejected" && x.reject_reason) info.push("Ret nedeni: " + esc(x.reject_reason));
      let btns = "";
      if (x.status !== "approved") {
        btns += '<button type="button" class="bb-btn-ok" style="padding:6px 12px;font-size:13px" data-act="camp-ok" data-id="' + c.id +
          '" data-camp="' + x.campaign_id + '">✅ Doğrulandı · +' + esc(String(x.reward_stamps)) + " Damga Ver</button>";
        if (x.status === "pending" || x.status === "joined") {
          btns += '<button type="button" class="bb-btn" style="padding:6px 12px;font-size:13px" data-act="camp-no" data-id="' + c.id +
            '" data-camp="' + x.campaign_id + '">Reddet</button>';
        }
      }
      return '<div class="bb-recent-row" style="align-items:flex-start">' +
        '<div style="flex:1 1 14rem;min-width:0"><div class="font-semibold text-on-surface text-sm">' + esc(x.title) + "</div>" +
        '<div class="bb-pill-row" style="margin-top:4px">' + window.campaignStatusPill(x.status) +
          pill("camp", "+" + x.reward_stamps + " damga") + (x.active ? "" : pill("none", "Kampanya pasif")) + "</div>" +
        (info.length ? '<div class="text-xs text-on-surface-variant" style="margin-top:4px">' + info.join(" · ") + "</div>" : "") +
        "</div>" +
        (btns ? '<div class="flex gap-2 flex-wrap">' + btns + "</div>" : "") +
      "</div>";
    }).join("");
    const pending = items.some((x) => x.status === "pending");
    return '<div class="bb-group"><div class="bb-label" style="margin:0">🎯 Ödüllü Kampanyalar' + (pending ? " — doğrulama bekleyen var" : "") + "</div>" + rows +
      '<div class="text-xs text-on-surface-variant">💬 Görevi (ör. Google yorumunu) müşterinin telefonunda gördükten sonra onaylayın. Onaylanmadan damga verilmez; her kampanyadan bir kez yararlanılır.</div></div>';
  };
  // Kasiyer uyarısı çıkınca kısa bir sesli ikaz (tarayıcı izin vermezse sessizce geçilir)
  window.proofBeep = function () {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx(), osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = "sine"; osc.frequency.value = 880; gain.gain.value = 0.08;
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(); osc.stop(ctx.currentTime + 0.18);
      osc.onended = () => ctx.close();
    } catch (e) {}
  };
})();
