"""
Sadakat Programı Koşulları (ADR-36, denetim Y-10).

Metin yalnızca sistemin gerçekte nasıl çalıştığını anlatır ve yapılandırılabilir değerleri (damga hedefi, ödül adı, günlük
limit, aynı ziyaret bekleme süresi) AYARLARDAN, her sayfa çizilişinde okur. Süre/fiyat/ticaret unvanı/adres UYDURULMAZ.
Hukuki metin değildir; avukat gözden geçirmesi önerilir.
"""
from __future__ import annotations

from typing import Mapping

from app.config import DEFAULT_BUSINESS_NAME
from app.services.retention import INACTIVE_CUSTOMER_DAYS


def _int_setting(settings: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(str(settings.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def build_program_terms(settings: Mapping[str, str]) -> str:
    """Müşteri uygulamasındaki "Sadakat Programı Koşulları" penceresinin düz metni (bölümler boş satırla ayrılır)."""
    brand = (settings.get("business_name") or "").strip() or DEFAULT_BUSINESS_NAME
    target = max(_int_setting(settings, "stamp_target", 5), 1)
    reward = (settings.get("reward_name") or "").strip() or "ödül"
    daily_limit = max(_int_setting(settings, "daily_limit", 2), 1)
    cooldown = max(_int_setting(settings, "min_interval_minutes", 0), 0)
    inactive_months = round(INACTIVE_CUSTOMER_DAYS / 365 * 12)

    earn = [
        f"• Kasadaki Damga QR'ını okutarak, kasadaki NFC etiketine dokunarak veya kasa personelinin kartına ekleyerek damga kazanırsın.",
        f"• Bir günde (Türkiye saati) en fazla {daily_limit} damga kazanabilirsin; kayıt hediyesi bu sınıra sayılmaz.",
    ]
    if cooldown > 0:
        earn.append(
            f"• QR/NFC ile aldığın bir damgadan sonra yeni bir QR/NFC damgası için {cooldown} dakika geçmesi gerekir "
            f"(aynı ziyarette tek QR damgası)."
        )
    earn.append(
        "• Her okutma bir kez geçerlidir: sayfayı yenilemek, ekran görüntüsü almak veya bağlantıyı paylaşmak ek damga vermez."
    )

    # ADR-38: uygulama / bildirim hediyesi panelden kapatılabilir; kapalı olan vaat edilmez
    gifts = ["• Yeni üyelik: kayıt olduğunda +1 hediye damga."]
    if (settings.get("bonus_install_enabled") or "1").strip() != "0":
        gifts.append("• Uygulamayı telefonunun ana ekranına eklediğinde +1 hediye damga.")
    if (settings.get("bonus_notif_enabled") or "1").strip() != "0":
        gifts.append("• Tarayıcı bildirimlerine izin verdiğinde +1 hediye damga. Bu hediye yalnızca tarayıcı iznine bağlıdır; "
                     "kampanya (ticari ileti) bildirimlerini onaylamana bağlı değildir.")
    if (settings.get("gift_requires_visit") or "1").strip() != "0":
        # ADR-39: hediyeler ilk kasa damgasıyla birlikte eklenir (uydurma numarayla hediye toplamaya karşı)
        gifts.append("Hediye damgalar, kasada aldığın ilk damgayla birlikte kartına eklenir.")
    gifts.append("Hesabını silip yeniden kayıt olmak ya da aynı telefonda başka numarayla hesap açmak hediye damgaların "
                 "yeniden verilmesini sağlamaz.")

    sections = [
        f"{brand} SADAKAT PROGRAMI KOŞULLARI",
        "Bu metin, uygulamadaki sadakat kartının nasıl işlediğini anlatır. Programa kayıt olarak bu koşulları kabul etmiş "
        "olursun; kayıt için ayrıca bir onay kutusu işaretlemen gerekmez.",
        "1. Damga kazanmak\n" + "\n".join(earn),
        "2. Hediye damgalar (her biri hesap başına bir kez)\n" + "\n".join(gifts),
        f"3. Ödül ve kullanımı\n"
        f"• {target} damgaya ulaştığında \"{reward}\" ödülü kazanırsın.\n"
        "• Ödülünü kasada, ödeme yaparken personele bildirerek (telefon numaranı veya kullanıcı adını söyleyerek) kullanırsın; "
        "personel onayladığında ödül kullanılmış sayılır.\n"
        "• Damgalar ve ödüller nakde çevrilemez, başkasına devredilemez veya satılamaz.",
        "4. Hesabın silinmesi ve hareketsizlik\n"
        "• Hesabını silersen damgaların ve kullanılmamış ödüllerin iptal edilir.\n"
        f"• {inactive_months} ay boyunca hiç işlem yapılmayan hesaplar otomatik olarak anonimleştirilir; bu durumda damga ve "
        "kullanılmamış ödüller de iptal olur.",
        "5. Kötüye kullanım\n"
        "• Sistemin kurallarını aşarak (ör. aynı bağlantıyı tekrar kullanarak, başkasının kartını veya sahte hesapları "
        "kullanarak) kazanılan damgalar incelenip iptal edilebilir.",
        f"6. Programın değişmesi veya sona ermesi\n"
        f"• {brand}, programın kurallarını (damga hedefi, ödül, günlük sınır, bekleme süresi gibi) değiştirebilir veya "
        "programı sona erdirebilir. Değişiklikler uygulamada duyurulur.",
        "7. Bu metin hakkında\n"
        "Bu koşullar uygulamada gösterilir ve güncellenebilir; geçerli olan, uygulamada o an görünen metindir. Kişisel "
        "verilerin nasıl işlendiği Aydınlatma Metni'nde anlatılır.",
    ]
    return "\n\n".join(sections)
