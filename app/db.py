import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Generator, Any
from app.config import (
    DB_PATH,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_BUSINESS_NAME,
    DEFAULT_REWARD_NAME,
    DEFAULT_STAMP_TARGET,
    get_edge_default_host,
)
from app.utils import get_istanbul_now_iso

# Önceki (v1) varsayılan aydınlatma metni: yönetici özelleştirmediyse migration ile v2'ye yükseltilir.
_KVKK_TEXT_V1 = (
    "KİŞİSEL VERİLERİN İŞLENMESİNE İLİŞKİN AYDINLATMA METNİ\n\n"
    f"{DEFAULT_BUSINESS_NAME} (\"Veri Sorumlusu\"), 6698 sayılı Kişisel Verilerin Korunması Kanunu'nun 10. maddesi "
    "kapsamında sizi bilgilendirir.\n\n"
    "İşlenen veriler: Cep telefonu numaranız, kullanıcı adınız, damga ve ödül hareketleriniz, tarayıcınızda "
    "saklanan rastgele cihaz belirteci, onay/rıza kayıtlarınız (zaman damgası ve IP adresi) ve — yalnızca bildirim "
    "izni verirseniz — tarayıcı bildirim uç noktanız.\n\n"
    "İşleme amaçları: Dijital sadakat kartı hizmetinin sunulması, damga ve ödüllerinizin takibi, hizmet "
    "güvenliğinin ve kötüye kullanımın önlenmesi. Kampanya/duyuru bildirimleri yalnızca bu metinden ayrı olarak "
    "vereceğiniz onay ile gönderilir; bu onayı dilediğiniz an geri alabilirsiniz.\n\n"
    "Aktarım: Verileriniz reklam veya pazarlama amacıyla üçüncü kişilerle paylaşılmaz. Verileriniz, hizmetin "
    "barındırıldığı sunucuda saklanır; uygulamanın yazı tipi ve yazılım dosyaları da aynı sunucudan sunulur.\n\n"
    "Haklarınız: KVKK m.11 kapsamındaki haklarınızı (bilgi talep etme, düzeltme, silme vb.) Profil sekmesindeki "
    "'Verilerimi ve Hesabımı Sil' seçeneğiyle veya aşağıdaki başvuru kanalı üzerinden kullanabilirsiniz."
)

# KVKK m.10 + Aydınlatma Yükümlülüğü Tebliği: veri sorumlusu, amaç + hukuki sebep, aktarım, toplama yöntemi, haklar.
# 2026/347 İlke Kararı: aydınlatma metni rıza/"kabul" ifadesi İÇERMEZ ve açık rıza metninden AYRI sunulur.
KVKK_TEXT_V2 = (
    "KİŞİSEL VERİLERİN İŞLENMESİNE İLİŞKİN AYDINLATMA METNİ\n\n"
    "Bu metin, 6698 sayılı Kişisel Verilerin Korunması Kanunu (KVKK) m.10 ve Aydınlatma Yükümlülüğünün Yerine "
    "Getirilmesinde Uyulacak Usul ve Esaslar Hakkında Tebliğ uyarınca sizi bilgilendirmek içindir; bir onay veya "
    "rıza metni değildir.\n\n"
    "1. Veri Sorumlusu\n"
    f"{DEFAULT_BUSINESS_NAME} (\"Veri Sorumlusu\"). Unvan, adres ve başvuru bilgileri metnin sonundadır.\n\n"
    "2. İşlenen Kişisel Veriler\n"
    "• Kimlik/iletişim: cep telefonu numarası, kullanıcı adı (isterseniz ad-soyad).\n"
    "• Müşteri işlem: damga, ödül ve hediye kullanım kayıtları ve tarihleri.\n"
    "• İşlem güvenliği: tarayıcınızda saklanan rastgele cihaz belirteci (sunucuda yalnızca özeti tutulur), "
    "IP adresi, işlem ve onay kayıtlarının zaman damgaları.\n"
    "• Yalnızca bildirimleri açarsanız: tarayıcı bildirim abonelik adresi ve bildirim tercihiniz.\n\n"
    "3. İşleme Amaçları ve Hukuki Sebepler\n"
    "• Sadakat kartı üyeliğinin kurulması, damga/ödül takibi ve hediyenin teslimi: KVKK m.5/2-c (sözleşmenin "
    "kurulması ve ifası).\n"
    "• Hizmet güvenliği, kötüye kullanımın (ör. hesabı silip yeniden kayıtla bonus) önlenmesi ve işlemlerin ispatı: "
    "KVKK m.5/2-f (meşru menfaat) ve m.5/2-e (bir hakkın tesisi, kullanılması veya korunması).\n"
    "• Kanuni yükümlülükler ve yetkili kurum talepleri: KVKK m.5/2-ç.\n"
    "• Kampanya/fırsat bildirimleri ve bu amaçla gruplandırma (ör. ödülüne az kalanlar): YALNIZCA ayrıca vereceğiniz "
    "\"Açık Rıza ve Ticari Elektronik İleti Onayı\" ile (KVKK m.5/1, 6563 sayılı Kanun m.6). Bu onayı vermemeniz "
    "sadakat kartını kullanmanıza engel değildir.\n"
    "Damga ve ödül durumunuza ilişkin bildirimler (\"Damgan eklendi\", \"Ödülün hazır\") reklam içermez ve yalnızca "
    "bildirimleri açtığınızda gönderilir.\n\n"
    "4. Toplama Yöntemi\n"
    "Veriler; uygulamaya girdiğiniz bilgiler, kasadaki QR kodu okutmanız veya kasiyerin sizin talebinizle yaptığı "
    "işlemler aracılığıyla elektronik ortamda, otomatik yollarla toplanır.\n\n"
    "5. Aktarım\n"
    "Verileriniz satılmaz ve reklam amacıyla üçüncü kişilerle paylaşılmaz; harici analitik/izleme aracı kullanılmaz. "
    "Veriler hizmetin barındırıldığı sunucuda saklanır (barındırma hizmeti sağlayıcısı); yazı tipleri ve yazılım "
    "dosyaları da aynı sunucudan sunulur. Mevzuat gereği talep halinde yetkili kamu kurumlarıyla paylaşılabilir. "
    "Bildirimleri açarsanız bildirimler, cihazınızın bildirim altyapısı (Google Firebase Cloud Messaging, Apple Push "
    "Notification Service veya Mozilla Push Service) üzerinden uçtan uca şifreli olarak iletilir; bu sağlayıcıların "
    "sunucuları yurt dışında bulunabilir ve bildirim içeriğini okuyamaz. Bu aktarım yalnızca bildirimleri açmanız "
    "halinde ve bildirimin size ulaştırılması amacıyla yapılır.\n\n"
    "6. Saklama Süreleri\n"
    "• Hesap verileri üyeliğiniz süresince saklanır; 24 ay boyunca hiç işlem yapılmayan hesaplar otomatik olarak "
    "anonimleştirilir.\n"
    "• Hesabınızı sildiğinizde telefon, isim, cihaz ve bildirim kayıtlarınız anında anonimleştirilir; kötüye "
    "kullanımı önlemek için yalnızca telefon numaranızın ve cihazınızın geri döndürülemez özeti (hash) tutulur.\n"
    "• Onay/ret ve güvenlik kayıtları ispat amacıyla 3 yıl saklanıp silinir.\n"
    "• Kapatılmış bildirim abonelikleri 90 gün sonra silinir.\n\n"
    "7. Çerezler ve Yerel Depolama\n"
    "Yalnızca zorunlu bir oturum çerezi (sadakat_device_token, 1 yıl) ve tarayıcının yerel deposunda aynı cihaz "
    "belirteci kullanılır; amacı, sizi her ziyarette telefon numarası sormadan tanımaktır. Reklam, analitik veya "
    "üçüncü taraf çerezi kullanılmaz.\n\n"
    "8. Haklarınız (KVKK m.11)\n"
    "Verilerinizin işlenip işlenmediğini öğrenme, bilgi talep etme, amacına uygun kullanılıp kullanılmadığını öğrenme, "
    "aktarıldığı üçüncü kişileri bilme, eksik/yanlış işlenmişse düzeltilmesini, silinmesini veya yok edilmesini ve "
    "bunların aktarılan kişilere bildirilmesini isteme, otomatik analiz sonucu aleyhinize bir sonuca itiraz etme ve "
    "kanuna aykırı işleme nedeniyle zararın giderilmesini talep etme haklarınız vardır. Kullanıcı adınızı Profil "
    "sekmesinden düzeltebilir, \"Verilerimi ve Hesabımı Sil\" ile hesabınızı anında silebilir, kampanya "
    "bildirimlerini Profil › Bildirimler anahtarıyla dilediğiniz an kapatabilirsiniz. Diğer talepleriniz için "
    "aşağıdaki başvuru kanalını kullanabilirsiniz; başvurular en geç 30 gün içinde ücretsiz sonuçlandırılır. "
    "Başvurunuz reddedilir veya yanıtsız kalırsa Kişisel Verileri Koruma Kurulu'na şikâyette bulunabilirsiniz."
)

# v3 (ADR-23): açık rıza + ticari ileti onayı kayıt ekranında ZORUNLU kutuyla alınır; uygulamada bildirim kapatma
# anahtarı yoktur. "İsteğe bağlı" ve "Profil › Bildirimler'den kapatabilirsiniz" ifadeleri artık doğru olmadığı için
# değişti; onayın geri alınabileceği (ETK m.8) yasal bilgi olarak kalır. Özelleştirilmemiş v1/v2 açılışta v3'e yükseltilir.
KVKK_TEXT_V3 = (
    KVKK_TEXT_V2
    .replace(
        "YALNIZCA ayrıca vereceğiniz \"Açık Rıza ve Ticari Elektronik İleti Onayı\" ile (KVKK m.5/1, 6563 sayılı Kanun "
        "m.6). Bu onayı vermemeniz sadakat kartını kullanmanıza engel değildir.",
        "kayıt sırasında ayrıca onayladığınız \"Açık Rıza ve Ticari Elektronik İleti Onay Metni\"ne dayanır (KVKK m.5/1, "
        "6563 sayılı Kanun m.6).",
    )
    .replace("• Kapatılmış bildirim abonelikleri 90 gün sonra silinir.",
             "• Pasif hale gelen bildirim abonelikleri 90 gün sonra silinir.")
    .replace(
        "ile hesabınızı anında silebilir, kampanya bildirimlerini Profil › Bildirimler anahtarıyla dilediğiniz an "
        "kapatabilirsiniz. Diğer talepleriniz için",
        "ile hesabınızı anında silebilirsiniz. Açık rızanızı ve ticari elektronik ileti onayınızı dilediğiniz an, "
        "ücretsiz ve gerekçe göstermeden geri alabilirsiniz (6563 sayılı Kanun m.8). Bu ve diğer talepleriniz için",
    )
)

# v4 (ADR-24): yasal düzeltme. Açık rıza hizmet şartı yapılamaz → isteğe bağlıdır; ret (ETK m.8) uygulama içinden
# Profil › "Kampanya Bildirimleri" anahtarıyla yapılır. V2 metnine dönülür, anahtar adı güncel haliyle yazılır.
KVKK_TEXT_V4 = KVKK_TEXT_V2.replace(
    "Profil › Bildirimler anahtarıyla", "Profil sekmesindeki \"Kampanya Bildirimleri\" anahtarıyla"
)


def _replace_checked(text: str, old: str, new: str) -> str:
    """`.replace()` sessizce boşa düşmesin: eski ifade metinde yoksa import anında hata verir (testler yakalar)."""
    if old not in text:
        raise AssertionError(f"Aydınlatma metni v5 üretilemedi, ifade bulunamadı: {old[:60]!r}")
    return text.replace(old, new, 1)


# v5 (ADR-34, denetim Y-06/Y-07/Y-09): kodun gerçekte yaptığı işlemlerin sade anlatımı. Yeni hüküm/rıza yoktur:
# işlem ve açılış kayıtları + "olası kötüye kullanım" notları, silme sonrası kalan özet, tüm çerez/yerel depo anahtarları,
# "Verilerimi İndir" hakkı. Özelleştirilmemiş v1-v4 açılışta v5'e yükseltilir; yeni teyitler v5 olarak kaydedilir.
KVKK_TEXT_V5 = KVKK_TEXT_V4
for _old, _new in (
    (
        "• Yalnızca bildirimleri açarsanız: tarayıcı bildirim abonelik adresi ve bildirim tercihiniz.\n",
        "• Yalnızca bildirimleri açarsanız: tarayıcı bildirim abonelik adresi ve bildirim tercihiniz.\n"
        "• İşlem kayıtları: damga isteklerinizin sonucu (verildi/verilmedi ve nedeni), hangi yolla yapıldığı (QR, NFC, "
        "kasa, uygulama), sayfanın açılış biçimi (yeni açılış, yenileme, geri tuşu), uygulamanın ana ekrana yüklü olup "
        "olmadığı ve bağlantının bir sosyal medya uygulamasının içinden açılıp açılmadığı gibi teknik bilgiler.\n"
        "• Olası kötüye kullanım notları: sistem, olağan dışı görünen durumları (ör. aynı bağlantının tekrar kullanılması, "
        "aynı telefonda farklı numarayla hesap) işlem kaydına \"olası kötüye kullanım\" diye not eder ve şüphe puanı "
        "biriktirir. Sistem bu notlarla hesabınızı kendiliğinden kapatmaz; notları yalnızca yetkili işletme çalışanları "
        "görür, damgaların iptali gibi kararları işletme yetkilisi verir.\n"
        "• Ödüllü kampanyalara katılırsanız: yorumda görünen adınız.\n",
    ),
    (
        "• Hesabınızı sildiğinizde telefon, isim, cihaz ve bildirim kayıtlarınız anında anonimleştirilir; kötüye "
        "kullanımı önlemek için yalnızca telefon numaranızın ve cihazınızın geri döndürülemez özeti (hash) tutulur.\n",
        "• Hesabınızı sildiğinizde telefon, isim, cihaz ve bildirim kayıtlarınız anında anonimleştirilir; damgalarınız ve "
        "ödülleriniz iptal edilir. Hesabı silip yeniden kayıtla hediye alınmasını önlemek için telefon numaranızın ve "
        "cihazınızın anahtarlı özeti (HMAC) tutulmaya devam eder; numaranızı bilen işletme bu özeti sizinle "
        "eşleştirebileceğinden bu özet de kişisel veridir ve bunun için ayrıca bir saklama süresi sınırı "
        "uygulanmamaktadır.\n",
    ),
    (
        "Yalnızca zorunlu bir oturum çerezi (sadakat_device_token, 1 yıl) ve tarayıcının yerel deposunda aynı cihaz "
        "belirteci kullanılır; amacı, sizi her ziyarette telefon numarası sormadan tanımaktır. Reklam, analitik veya "
        "üçüncü taraf çerezi kullanılmaz.",
        "Yalnızca uygulamanın çalışması için zorunlu olanlar kullanılır: (1) sadakat_device_token çerezi (1 yıl) ve "
        "tarayıcının yerel deposunda (loyalty_device_token) aynı cihaz belirteci: sizi her ziyarette telefon numarası "
        "sormadan tanımak için; (2) sadakat_stamp_hold çerezi (10 dakika): kayıt formunu doldururken damga kodunun "
        "yalnızca sizin tarayıcınıza ayrılması için; (3) tarayıcının oturum deposunda m1_notif_offer_dismissed: "
        "bildirim önerisinin kapatıldığını hatırlamak için (sekme kapanınca silinir). Kasa/yönetim panelini ve kasa QR "
        "ekranını kullanan yetkili çalışanlarda ayrıca oturum ve eşleştirme çerezleri kullanılır. Reklam, analitik "
        "veya üçüncü taraf çerezi kullanılmaz.",
    ),
    (
        "\"Verilerimi ve Hesabımı Sil\" ile hesabınızı anında silebilir,",
        "\"Verilerimi İndir\" ile hesabınıza bağlı kayıtların bir kopyasını indirebilir, \"Verilerimi ve Hesabımı Sil\" "
        "ile hesabınızı anında silebilir,",
    ),
):
    KVKK_TEXT_V5 = _replace_checked(KVKK_TEXT_V5, _old, _new)
del _old, _new

# v6 (ADR-36, denetim Y-03): uygulamaya erişimin Cloudflare, Inc. (ABD) altyapısı üzerinden sağlanabildiğinin düz anlatımı.
# Aktarımın hukuki dayanağı veya bir sözleşme iddiası YOKTUR; yalnızca olgu: IP ve istek verileri o sağlayıcıdan da geçer.
_EDGE_HOST_NOTE = f" ({get_edge_default_host()} adresi)" if get_edge_default_host() else ""
KVKK_TEXT_V6 = _replace_checked(
    KVKK_TEXT_V5,
    "dosyaları da aynı sunucudan sunulur. Mevzuat gereği",
    f"dosyaları da aynı sunucudan sunulur. Uygulamaya erişim, Cloudflare, Inc. (ABD) altyapısı{_EDGE_HOST_NOTE} "
    "üzerinden sağlanabilir; bu durumda IP adresiniz ve uygulamaya gönderdiğiniz istek verileri (ör. cihaz "
    "belirteciniz, telefon numaranız ve işlem bilgileri gibi istekle iletilen bilgiler) bu sağlayıcının altyapısından "
    "geçer ve bu sağlayıcı tarafından da işlenir; bu işleme yurt dışına aktarım içerebilir. Mevzuat gereği",
)

# v7 (ADR-47): kayıtta ad-soyad zorunlu oldu; aydınlatma metni bunu ve amacını açıkça söyler.
KVKK_TEXT_V7 = _replace_checked(
    KVKK_TEXT_V6,
    "• Kimlik/iletişim: cep telefonu numarası, kullanıcı adı (isterseniz ad-soyad).\n",
    "• Kimlik/iletişim: ad-soyad ve cep telefonu numarası (kayıtta istenir; kartın size ait olduğunu kasada "
    "doğrulamak ve size adınızla hitap etmek için), Profil'den değiştirebileceğiniz görünen ad.\n",
)

# v2 (ADR-23, ZORUNLU kutu; yasal bulunmadığı için ADR-24'te yerini v3'e bıraktı): özelleştirilmemişse v3'e yükseltilir.
_MARKETING_CONSENT_TEXT_V2 = (
    "AÇIK RIZA VE TİCARİ ELEKTRONİK İLETİ ONAY METNİ\n\n"
    f"Bu metni onaylamanızla {DEFAULT_BUSINESS_NAME}:\n"
    "• size kampanya, indirim, yeni ürün ve fırsat duyurularını uygulama/tarayıcı bildirimi (web push) yoluyla "
    "gönderebilir (6563 sayılı Elektronik Ticaretin Düzenlenmesi Hakkında Kanun m.6 — ticari elektronik ileti onayı);\n"
    "• bu bildirimleri size uygun hale getirmek için damga sayınız, bekleyen ödülünüz ve son ziyaret tarihiniz gibi "
    "verilerinizle sizi kampanya gruplarına (ör. \"ödülüne 1-2 damga kalanlar\", \"30 gündür gelmeyenler\") dahil "
    "edebilir (KVKK m.5/1 — açık rıza);\n"
    "• bildirimin cihazınıza ulaşması için bildirim abonelik adresiniz, cihazınızın bildirim altyapısı sağlayıcısına "
    "(Google, Apple veya Mozilla; sunucuları yurt dışında olabilir) uçtan uca şifreli olarak iletilir.\n\n"
    "Bildirimler yalnızca cihazınızda bildirim izni verdiğinizde gönderilir. Onayınızı dilediğiniz an, ücretsiz ve "
    "gerekçe göstermeden geri alabilirsiniz (6563 sayılı Kanun m.8); bunun için Aydınlatma Metni'nde yer alan başvuru "
    "kanalından bize ulaşmanız yeterlidir. Onay ve ret kayıtlarınız (tarih-saat, IP adresi, metin sürümü) ispat "
    "amacıyla 3 yıl saklanır. Ayrıntılar için Aydınlatma Metni'ne bakınız."
)

# v3 (ADR-24; ADR-27'de yerini v4'e bıraktı): ret uygulama içinden VE kampanya bildirimindeki "Bildirimleri kapat"
# düğmesinden yapılabiliyordu. Özelleştirilmemişse açılışta v4'e yükseltilir.
_MARKETING_CONSENT_TEXT_V3 = (
    "AÇIK RIZA VE TİCARİ ELEKTRONİK İLETİ ONAY METNİ\n\n"
    "Bu onay isteğe bağlıdır; vermemeniz sadakat kartını ve hediyelerinizi kullanmanızı etkilemez.\n\n"
    f"Onay vermeniz halinde {DEFAULT_BUSINESS_NAME}:\n"
    "• size kampanya, indirim, yeni ürün ve fırsat duyurularını uygulama/tarayıcı bildirimi (web push) yoluyla "
    "gönderebilir (6563 sayılı Elektronik Ticaretin Düzenlenmesi Hakkında Kanun m.6 — ticari elektronik ileti onayı);\n"
    "• bu bildirimleri size uygun hale getirmek için damga sayınız, bekleyen ödülünüz ve son ziyaret tarihiniz gibi "
    "verilerinizle sizi kampanya gruplarına (ör. \"ödülüne 1-2 damga kalanlar\", \"30 gündür gelmeyenler\") dahil "
    "edebilir (KVKK m.5/1 — açık rıza);\n"
    "• bildirimin cihazınıza ulaşması için bildirim abonelik adresiniz, cihazınızın bildirim altyapısı sağlayıcısına "
    "(Google, Apple veya Mozilla; sunucuları yurt dışında olabilir) uçtan uca şifreli olarak iletilir.\n\n"
    "Onayınızı dilediğiniz an, ücretsiz ve gerekçe göstermeden geri alabilirsiniz (6563 sayılı Kanun m.8): uygulamada "
    "Profil › \"Kampanya Bildirimleri\" anahtarını kapatmanız veya kampanya bildirimindeki \"Bildirimleri kapat\" "
    "düğmesine dokunmanız yeterlidir; ret anında işlenir. Onay ve ret kayıtlarınız (tarih-saat, IP adresi, metin "
    "sürümü) ispat amacıyla 3 yıl saklanır. Ayrıntılar için Aydınlatma Metni'ne bakınız."
)

# Aydınlatmadan AYRI metin: KVKK açık rızası (pazarlama amaçlı işleme + gruplandırma) ve 6563 s. Kanun m.6 ticari
# elektronik ileti onayı. İsteğe bağlı; kayıt ekranındaki işaretsiz kutu bu metne bağlıdır. v4 (ADR-27): bildirimler
# "kapat" satırı/düğmesi taşımaz → ret yolu yalnızca uygulamadaki Profil › "Kampanya Bildirimleri" anahtarıdır.
MARKETING_CONSENT_TEXT = _MARKETING_CONSENT_TEXT_V3.replace(
    " anahtarını kapatmanız veya kampanya bildirimindeki \"Bildirimleri kapat\" düğmesine dokunmanız yeterlidir;",
    " anahtarını kapatmanız yeterlidir;",
)

# v1 varsayılanı: özelleştirilmemişse açılışta güncel sürüme yükseltilir.
_MARKETING_CONSENT_TEXT_V1 = (
    "AÇIK RIZA VE TİCARİ ELEKTRONİK İLETİ ONAY METNİ\n\n"
    "Bu onay isteğe bağlıdır; vermemeniz sadakat kartını ve hediyelerinizi kullanmanızı etkilemez.\n\n"
    f"Onay vermeniz halinde {DEFAULT_BUSINESS_NAME}:\n"
    "• size kampanya, indirim, yeni ürün ve fırsat duyurularını uygulama/tarayıcı bildirimi (web push) yoluyla "
    "gönderebilir (6563 sayılı Elektronik Ticaretin Düzenlenmesi Hakkında Kanun m.6 — ticari elektronik ileti onayı);\n"
    "• bu bildirimleri size uygun hale getirmek için damga sayınız, bekleyen ödülünüz ve son ziyaret tarihiniz gibi "
    "verilerinizle sizi kampanya gruplarına (ör. \"ödülüne 1-2 damga kalanlar\", \"30 gündür gelmeyenler\") dahil "
    "edebilir (KVKK m.5/1 — açık rıza);\n"
    "• bildirimin cihazınıza ulaşması için bildirim abonelik adresiniz, cihazınızın bildirim altyapısı sağlayıcısına "
    "(Google, Apple veya Mozilla; sunucuları yurt dışında olabilir) uçtan uca şifreli olarak iletilir.\n\n"
    "Onayınızı dilediğiniz an, ücretsiz ve gerekçe göstermeden geri alabilirsiniz: uygulamada Profil › Bildirimler "
    "anahtarını kapatmanız veya bildirimdeki \"Bildirimleri kapat\" düğmesine dokunmanız yeterlidir; ret anında "
    "işlenir. Onay ve ret kayıtlarınız (tarih-saat, IP adresi, metin sürümü) ispat amacıyla 3 yıl saklanır. "
    "Ayrıntılar için Aydınlatma Metni'ne bakınız."
)

# Kaldırılan ayarlar (ADR-21): dinamik/süreli QR ve müdürün düzenlediği müşteri ekranı çağrı metinleri.
# ADR-35: sistemi açan / kayıt yaptıran sabit QR'ın mührü (/m/kayit). Hiçbir koşulda damga vermez; devre dışı
# bırakılamaz, damga mührü olarak kullanılamaz. Damga QR'ı 'qr' mührüdür (/m/qr).
REGISTER_SEAL_TOKEN = "kayit"

REMOVED_SETTING_KEYS = (
    "notif_cta_title", "notif_cta_text", "install_cta_title", "install_cta_text",
    "dynamic_qr_enabled", "dynamic_qr_ttl",
    # Y-18: onaysız WhatsApp pazarlama listelerinin ölü kodu ve ayarı kaldırıldı
    "whatsapp_template",
)

DEFAULT_SETTINGS = {
    "business_name": DEFAULT_BUSINESS_NAME,
    "accent_color": DEFAULT_ACCENT_COLOR,
    "stamp_target": DEFAULT_STAMP_TARGET,
    "reward_name": DEFAULT_REWARD_NAME,
    "daily_limit": "2",
    # ADR-29: aynı müşteri QR / NFC ile art arda damga alamaz (son damgasından bu kadar dakika sonra); 0 = kapalı
    "min_interval_minutes": "60",
    # ADR-30: basılı (sabit) kasa QR'ı ve eski NFC etiketi de damga verir mi? "0" = yalnızca kayıt + uygulama; damga
    # NTAG 424 DNA etiketi, Kasa QR Ekranı, kasa panelinin canlı kodu ya da kasiyerle verilir.
    "static_qr_stamps": "1",
    # ADR-38: uygulamayı yükleme / bildirimleri açma karşılığı tek seferlik +1 hediye damga panelden açılıp kapatılır.
    # Kapalıyken hak kullanılmış sayılmaz (yeniden açılınca müşteri alabilir).
    "bonus_install_enabled": "1",
    "bonus_notif_enabled": "1",
    # ADR-39: hediye damgaları (kayıt / yükleme / bildirim) ilk gerçek kasada verilen damgayla birlikte eklenir;
    # evden rastgele numarayla açılan hesap hediye toplayamaz. "0" = eski davranış (hemen verilir).
    "gift_requires_visit": "1",
    # ADR-47: yeni müşteri kaydında ad-soyad zorunlu ("0" = eski davranış: yalnızca telefon, ad otomatik userN)
    "registration_requires_name": "1",
    "kvkk_text": KVKK_TEXT_V7,
    "kvkk_version": "2026-11-v7",
    "kvkk_contact": "",
    # Veri sorumlusunun tam unvanı + adresi (KVKK m.10/a): aydınlatma metninin sonuna eklenir; müdür panelinden girilir.
    "kvkk_controller": "",
    "kvkk_address": "",   # Veri sorumlusunun açık adresi (müdür panelinden girilir; boşsa metne eklenmez)
    "marketing_consent_text": MARKETING_CONSENT_TEXT,
    "marketing_consent_version": "2026-11-v4",
    "logo_url": "",
    "menu_note": "Menüdeki fiyatlar yalnızca bilgilendirme amaçlıdır. Uygulama üzerinden online ödeme veya sipariş alınmaz; ödeme işletmede yapılır."
}


def default_message_templates(brand: str) -> list[tuple[str, str, str, str]]:
    """İlk kurulumda tohumlanan hazır şablonlar: (kind, düğme etiketi, başlık, metin). Sonradan panelden yönetilir."""
    return [
        ("campaign", "☕ Hafta Sonu: 2. Kahve %50 İndirimli",
         "☕ Hafta Sonu Kahve Saati: İkinci Kahven %50 İndirimli!",
         "Cumartesi ve Pazar günleri terasımızda sevdiklerinizle paylaştığınız ikinci kahveye özel ayrıcalık. "
         "Tüm üyelerimiz için geçerlidir."),
        ("campaign", "✨ Sabah Ritüeli: Kruvasan + Flat White",
         "✨ Sabah Ritüeli: Kruvasan + Flat White Eşleşmesi",
         "Hafta içi 08:30 - 11:30 saatleri arasında taze fırınlanmış tereyağlı kruvasan ve flat white eşleşmesi."),
        ("campaign", "🎉 8. Damgaya Özel Artisan Cheesecake",
         "🎉 8. Damgaya Özel Artisan Cheesecake",
         f"{brand} sadakat kartını tamamlayan tüm misafirlerimize kahvesinin yanında artisan tatlı ikramımız."),
        ("push", "☕ Günaydın! Harika bir espresso ile başlamaya ne dersin?", brand,
         f"☕ Günaydın {brand} ailesi! Güne harika bir espresso ile başlamaya ne dersin?"),
        ("push", "🎁 Tebrikler! Damgan tamamlandı, ücretsiz kahven hazır.", brand,
         "🎁 Tebrikler! Damgan tamamlandı, ücretsiz artisan kahven seni bekliyor."),
        ("push", "🥐 Sıcak Çıktı: Taze tereyağlı kruvasanlar fırından çıktı!", brand,
         "🥐 Sıcak Çıktı: Taze tereyağlı kruvasanlarımız fırından çıktı!"),
    ]


def default_reward_campaign(brand: str) -> tuple[str, str, str, str]:
    """Hazır ödüllü görev (ADR-26): (başlık, açıklama, rozet, düğme metni). Ödül puana bağlı DEĞİLDİR."""
    return (
        "⭐ Google'da Yorum Yap, +1 Damga Kazan",
        f"{brand} deneyimini Google'da paylaş. Yorumunu yazdıktan sonra buraya dönüp \"Yorumumu Yaptım\"a dokun; "
        "yorumun kontrol edilince kartına +1 damga eklenir. Puanın ne olursa olsun teşekkür hediyemiz geçerlidir.",
        "ÖDÜLLÜ",
        "Google'da Yorum Yap",
    )


def get_connection(db_path: Any = None) -> sqlite3.Connection:
    """SQLite veritabanı bağlantısı açar, WAL modu ve foreign key desteğini etkinleştirir."""
    if db_path is None:
        import app.config
        db_path = app.config.DB_PATH
    conn = sqlite3.connect(db_path, timeout=10.0, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.db_path = db_path
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


class _Connection(sqlite3.Connection):
    """`get_db` bağlamında yazma kilidi bırakıldıktan SONRA çalışacak işleri (ör. ağ üzerinden push gönderimi) tutar."""
    db_path: Any = None
    defer_ok = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.after_commit: list = []


def run_after_commit(conn: sqlite3.Connection, fn) -> bool:
    """
    `fn(db_path)` işini işlem COMMIT edilip bağlantı kapandıktan sonra çalıştırmak üzere sıraya alır (yazma kilidi tutulurken
    ağ çağrısı yapılmasın: yavaş push sunucusu tüm yazmaları kilitlemesin). `get_db` dışındaki bağlantıda sıraya alınamaz:
    False döner ve çağıran işi hemen yapmalıdır. İşlem geri alınırsa iş çalıştırılmaz.
    """
    if getattr(conn, "defer_ok", False):
        conn.after_commit.append(fn)
        return True
    return False


@contextmanager
def get_db(immediate: bool = False, db_path: Any = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Veritabanı bağlantısı ve transaction yönetim bağlamı.
    immediate=True ise eşzamanlı çift tıklama ve yarış durumlarını önlemek için BEGIN IMMEDIATE çalıştırır.
    `run_after_commit` ile sıraya alınan işler, başarılı COMMIT ve bağlantı kapanışından sonra (yazma kilidi yokken) çalışır.
    """
    conn = get_connection(db_path)
    conn.defer_ok = True
    committed = False
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
        committed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        callbacks = list(conn.after_commit) if committed else []
        path = conn.db_path
        conn.close()
    for callback in callbacks:
        try:
            callback(path)
        except Exception as exc:  # işlem zaten kesinleşti; gönderim hatası isteği bozmaz
            logging.getLogger("sadakat").warning("COMMIT sonrası iş başarısız (yutuldu): %s", exc)


def _ensure_columns(cursor: sqlite3.Cursor, table: str, expected_columns: list[tuple[str, str]]) -> set[str]:
    """Tablodaki mevcut sütunları kontrol eder ve eksik olanları veri silmeden ALTER TABLE ile ekler."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cursor.fetchall()}
    for col_name, col_def in expected_columns:
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
            existing.add(col_name)
    return existing


DEFAULT_CATEGORIES = ["Sıcak Kahveler", "Soğuk Kahveler", "Tatlılar", "Atıştırmalıklar"]

# Başlangıç hesapları (müdür panelinden değiştirilebilir). Parolalar yalnızca PBKDF2 özeti olarak saklanır.
# Kaynak kodda parola YOKTUR (depoyu gören herkes bilirdi): ilk parola sunucu .env'deki MANAGER_PASSWORD /
# STAFF_PASSWORD'den gelir; MANAGER_PASSWORD yoksa rastgele üretilip sunucu günlüğüne (docker logs) bir kez yazılır.
DEFAULT_MANAGER = ("mudur", "Müdür")
DEFAULT_STAFF = ("kasa1", "Kasa 1")
_SEED_HASH_CACHE: dict[str, str] = {}


def _seed_hash(password: str) -> str:
    from app.services.staff import hash_password
    if password not in _SEED_HASH_CACHE:
        _SEED_HASH_CACHE[password] = hash_password(password)
    return _SEED_HASH_CACHE[password]


def _usable_initial_password(raw: str) -> str:
    """Ortamdan gelen başlangıç parolası en az 8 karakter ve herkesçe bilinen eski varsayılan değilse kullanılır."""
    from app.services.accounts import MIN_PASSWORD_LEN, is_legacy_default_password
    pw = (raw or "").strip()
    return pw if len(pw) >= MIN_PASSWORD_LEN and not is_legacy_default_password(pw) else ""


def _seed_default_accounts(cursor: sqlite3.Cursor, now_iso: str) -> None:
    """
    Müdür hesabı yoksa 'mudur' eklenir: parola MANAGER_PASSWORD'den, yoksa rastgele (günlüğe bir kez yazılır).
    'kasa1' kasiyeri yalnızca STAFF_PASSWORD tanımlıysa ve yalnızca bir kez tohumlanır (silinirse geri gelmez);
    tanımlı değilse müdür, Personel sekmesinden kasiyer hesabı açar.
    """
    from app.config import get_manager_password, get_staff_password
    cursor.execute("SELECT COUNT(*) AS c FROM admin_users WHERE role = 'manager'")
    if cursor.fetchone()["c"] == 0:
        user, disp = DEFAULT_MANAGER
        pw = _usable_initial_password(get_manager_password())
        generated = not pw
        if generated:
            import secrets
            pw = secrets.token_urlsafe(12)
        cursor.execute(
            "INSERT OR IGNORE INTO admin_users (username, password_hash, role, display_name, active, created_at, created_by) "
            "VALUES (?, ?, 'manager', ?, 1, ?, 'sistem')",
            (user, _seed_hash(pw), disp, now_iso),
        )
        if generated and cursor.rowcount:
            logging.getLogger("sadakat").warning(
                "İlk müdür hesabı oluşturuldu — kullanıcı adı: %s, geçici parola: %s . İlk girişte Ayarlar › Hesabım'dan "
                "değiştirin (kalıcı başlangıç parolası için sunucu .env dosyasına MANAGER_PASSWORD yazılabilir).", user, pw,
            )
    cursor.execute("SELECT value FROM settings WHERE key = 'default_staff_seeded'")
    if not cursor.fetchone():
        pw = _usable_initial_password(get_staff_password())
        if pw:
            user, disp = DEFAULT_STAFF
            cursor.execute(
                "INSERT OR IGNORE INTO admin_users (username, password_hash, role, display_name, active, created_at, created_by) "
                "VALUES (?, ?, 'staff', ?, 1, ?, 'sistem')",
                (user, _seed_hash(pw), disp, now_iso),
            )
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('default_staff_seeded', '1')")


def _backfill_bonus_proof(cursor: sqlite3.Cursor) -> None:
    """
    Kasa "kanıt tablosu" için eski kayıtları bir kez doldurur (denetim günlüğünden):
    indirme/bildirim bonusu zamanları, kara listeden dönen hesaplar, kayıt anı (hoş geldin) damgası.
    """
    import re

    if cursor.execute("SELECT 1 FROM settings WHERE key = 'bonus_proof_backfill_v1'").fetchone():
        return
    for a in cursor.execute(
        "SELECT at, actor, action FROM audit_log WHERE action IN ('pwa_bonus_claimed', 'notif_bonus_claimed') ORDER BY id"
    ).fetchall():
        m = re.match(r"customer:(\d+)", a["actor"] or "")
        if not m:
            continue
        cid = int(m.group(1))
        col, src = ("pwa_bonus_at", "bonus_pwa") if a["action"] == "pwa_bonus_claimed" else ("push_bonus_at", "bonus_notif")
        cursor.execute(f"UPDATE customers SET {col} = ? WHERE id = ? AND {col} IS NULL", (a["at"], cid))
        st = cursor.execute(
            "SELECT id FROM stamps WHERE customer_id = ? AND source IS NULL AND ABS(julianday(created_at) - julianday(?)) * 86400 <= 5 "
            "ORDER BY ABS(julianday(created_at) - julianday(?)) LIMIT 1",
            (cid, a["at"], a["at"]),
        ).fetchone()
        if st:
            cursor.execute("UPDATE stamps SET source = ? WHERE id = ?", (src, st["id"]))
    for a in cursor.execute("SELECT actor FROM audit_log WHERE action = 'returning_deleted_identity'").fetchall():
        m = re.match(r"customer:(\d+)", a["actor"] or "")
        if m:
            cursor.execute("UPDATE customers SET is_re_registered = 1 WHERE id = ?", (int(m.group(1)),))
    for a in cursor.execute("SELECT detail FROM audit_log WHERE action = 'customer_registered'").fetchall():
        m = re.search(r"#(\d+)", a["detail"] or "")
        if not m:
            continue
        cid = int(m.group(1))
        st = cursor.execute(
            "SELECT s.id, s.created_at FROM stamps s JOIN customers c ON c.id = s.customer_id "
            "WHERE s.customer_id = ? AND s.source IS NULL AND (julianday(s.created_at) - julianday(c.created_at)) * 86400 BETWEEN -2 AND 20 "
            "ORDER BY s.id LIMIT 1",
            (cid,),
        ).fetchone()
        if st:
            cursor.execute("UPDATE stamps SET source = 'welcome' WHERE id = ?", (st["id"],))
            cursor.execute(
                "UPDATE customers SET welcome_bonus_awarded = 1, welcome_bonus_at = COALESCE(welcome_bonus_at, ?) WHERE id = ?",
                (st["created_at"], cid),
            )
    cursor.execute(
        "UPDATE customers SET welcome_bonus_at = (SELECT MIN(created_at) FROM stamps WHERE customer_id = customers.id AND source = 'welcome') "
        "WHERE welcome_bonus_awarded = 1 AND welcome_bonus_at IS NULL"
    )
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('bonus_proof_backfill_v1', '1')")


# ADR-36: "Bildirimleri Aç" düğmesinin kampanya (ticari ileti) onayını da yazdığı dönemde (ADR-23 sonrası) verilmiş onayların
# kanal etiketi. Bu etiket YALNIZCA o düğme akışından üretildi (kayıt kutusu "kayıt formu", Profil anahtarı
# "site içi bildirim anahtarı" yazar); bu yüzden ayırt edilebilir. Hem müşteri satırındaki kanal sütununda (v5 sonrası) hem de
# denetim günlüğündeki "kanal=bildirim izni" metninde (öncesi) bulunur.
BUNDLED_CONSENT_CHANNEL_PREFIX = "bildirim izni"
RECONFIRM_CHANNEL_LABEL = "yeniden onay gerekli (eski bildirim izni onayı)"


def reconfirm_bundled_marketing_consents(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Kampanya onayı "Bildirimleri Aç" düğmesiyle paketli verilmiş müşterileri bulur ve onayı GEÇERSİZ sayar:
    marketing_consent = 0 (böylece tüm kitle/sayaç/gönderim sorguları onları otomatik dışarıda bırakır) ve
    marketing_reconfirm_pending = 1 (uygulama bir kez sakin bir kartla Evet/Hayır sorar). Açık kutudan (kayıt formu) ya da
    Profil anahtarından verilen onaylara dokunulmaz. Kanal belirlenemeyen onaylar (ne sütunda ne günlükte iz var) olduğu gibi
    bırakılır. Sonuç: {"checked", "reconfirm", "kept_explicit", "unknown"}. İşlem idempotenttir (yalnızca marketing_consent=1 olanlara bakar).
    """
    counts = {"checked": 0, "reconfirm": 0, "kept_explicit": 0, "unknown": 0}
    now_iso = get_istanbul_now_iso()
    rows = conn.execute(
        "SELECT id, marketing_consent_channel AS channel FROM customers "
        "WHERE deleted_at IS NULL AND COALESCE(marketing_consent, 0) = 1"
    ).fetchall()
    for row in rows:
        counts["checked"] += 1
        channel = (row["channel"] or "").strip()
        source = "sütun"
        if not channel:
            # v5 öncesi onaylarda kanal yalnızca denetim günlüğündeki serbest metindedir: en son "verildi" kaydına bakılır
            log = conn.execute(
                "SELECT detail FROM audit_log WHERE actor = ? AND action = 'marketing_consent_given' ORDER BY id DESC LIMIT 1",
                (f"customer:{row['id']}",),
            ).fetchone()
            detail = (log["detail"] if log else "") or ""
            marker = "kanal="
            if marker not in detail:
                counts["unknown"] += 1
                continue
            channel = detail.split(marker, 1)[1].strip()
            source = "denetim günlüğü"
        if channel.lower().startswith(BUNDLED_CONSENT_CHANNEL_PREFIX):
            conn.execute(
                "UPDATE customers SET marketing_consent = 0, marketing_consent_at = ?, explicit_consent_at = NULL, "
                "marketing_consent_channel = ?, marketing_reconfirm_pending = 1 WHERE id = ?",
                (now_iso, RECONFIRM_CHANNEL_LABEL, row["id"]),
            )
            conn.execute(
                "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
                (now_iso, f"customer:{row['id']}", "marketing_consent_reconfirm_required",
                 f"Kampanya onayı 'Bildirimleri Aç' düğmesiyle paketli verilmişti (kaynak={source}); geçersiz sayıldı, "
                 f"müşteriden uygulama içi kartla yeniden onay istenecek (ADR-36)."),
            )
            counts["reconfirm"] += 1
        else:
            counts["kept_explicit"] += 1
    return counts


def init_db(db_path: Any = None) -> None:
    """Veritabanı tablolarını, otomatik şema migration'larını ve varsayılan ayarları oluşturur."""
    now_iso = get_istanbul_now_iso()
    with get_db(immediate=True, db_path=db_path) as conn:
        cursor = conn.cursor()

        # 1. Ayarlar Tablosu
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        _ensure_columns(cursor, "settings", [
            ("key", "TEXT"),
            ("value", "TEXT NOT NULL DEFAULT ''"),
        ])

        # 2. Müşteriler Tablosu
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_e164 TEXT UNIQUE NOT NULL,
                display_name TEXT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT NULL,
                push_opt_in INTEGER NOT NULL DEFAULT 0,
                push_permission_at TEXT NULL,
                pwa_bonus_claimed INTEGER NOT NULL DEFAULT 0
            );
        """)
        cust_cols = _ensure_columns(cursor, "customers", [
            ("phone_e164", "TEXT"),
            ("display_name", "TEXT NULL"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("deleted_at", "TEXT NULL"),
            ("push_opt_in", "INTEGER NOT NULL DEFAULT 0"),
            ("push_permission_at", "TEXT NULL"),
            ("pwa_bonus_claimed", "INTEGER NOT NULL DEFAULT 0"),
            # KVKK: aydınlatma metni görüldü / ayrı açık rıza kaydı (zaman damgası + IP + metin sürümü)
            ("kvkk_read_at", "TEXT NULL"),
            ("explicit_consent_at", "TEXT NULL"),
            ("consent_ip", "TEXT NULL"),
            ("consent_version", "TEXT NULL"),
            ("consent_source", "TEXT NULL"),
            # Ticari elektronik ileti (kampanya bildirimi) onayı: 0/1
            ("marketing_consent", "INTEGER NOT NULL DEFAULT 0"),
            ("marketing_consent_at", "TEXT NULL"),
            # Y-08: son onay/ret kararının kalıcı kaydı (metin sürümü, kanal, IP). Tarih = marketing_consent_at.
            ("marketing_consent_version", "TEXT NULL"),
            ("marketing_consent_channel", "TEXT NULL"),
            ("marketing_consent_ip", "TEXT NULL"),
            # ADR-36: "Bildirimleri Aç" düğmesiyle paketli verilmiş eski kampanya onayı geçersiz sayıldı → müşteriden bir kez
            # sakin kartla yeniden onay istenir (1 = kart gösterilecek). marketing_consent bu sırada 0'dır.
            ("marketing_reconfirm_pending", "INTEGER NOT NULL DEFAULT 0"),
            # Bildirimleri açma hediyesi (tek seferlik)
            ("notif_bonus_claimed", "INTEGER NOT NULL DEFAULT 0"),
            # Kayıt anı (hoş geldin) damgası verildi mi; şüphe puanı (dinamik QR ihlalleri) ve müdür incelemesi
            ("welcome_bonus_awarded", "INTEGER NOT NULL DEFAULT 0"),
            ("fraud_risk_score", "INTEGER NOT NULL DEFAULT 0"),
            ("fraud_flagged_at", "TEXT NULL"),
            ("fraud_reviewed_at", "TEXT NULL"),
            # Kasa "kanıt tablosu": tek seferlik bonusların ne zaman verildiği ve hesabın kara listeden dönüp dönmediği
            ("welcome_bonus_at", "TEXT NULL"),
            ("pwa_bonus_at", "TEXT NULL"),
            ("push_bonus_at", "TEXT NULL"),
            ("is_re_registered", "INTEGER NOT NULL DEFAULT 0"),
            # Canlı cihaz durumu: müşteri uygulamayı ana ekran (standalone) modunda en son ne zaman açtı
            ("pwa_standalone", "INTEGER NOT NULL DEFAULT 0"),
            ("pwa_seen_at", "TEXT NULL"),
            # ADR-39: hak kazanılmış ama ilk gerçek (kasada) damgaya kadar bekletilen hediye damgaları
            ("welcome_bonus_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("pwa_bonus_pending", "INTEGER NOT NULL DEFAULT 0"),
            ("notif_bonus_pending", "INTEGER NOT NULL DEFAULT 0"),
        ])
        cursor.execute("UPDATE customers SET pwa_bonus_claimed = 0 WHERE pwa_bonus_claimed IS NULL")
        if "phone" in cust_cols:
            cursor.execute(
                "UPDATE customers SET phone_e164 = phone WHERE (phone_e164 IS NULL OR phone_e164 = '') AND phone IS NOT NULL"
            )
        cursor.execute("UPDATE customers SET created_at = ? WHERE created_at IS NULL OR created_at = ''", (now_iso,))
        # İsim atanmamış tüm müşterilere otomatik user1, user2... ata
        cursor.execute(
            "UPDATE customers SET display_name = 'user' || id WHERE display_name IS NULL OR TRIM(display_name) = ''"
        )

        # 3. Cihazlar Tablosu (Cihaz anahtarının SHA-256 hash'i)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                token_hash TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
            );
        """)
        _ensure_columns(cursor, "devices", [
            ("customer_id", "INTEGER NOT NULL DEFAULT 0"),
            ("token_hash", "TEXT"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
        ])

        # 4. Mühürler Tablosu (QR / NFC kimlikleri)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
        """)
        _ensure_columns(cursor, "seals", [
            ("token", "TEXT"),
            ("label", "TEXT NOT NULL DEFAULT 'Kasa Mührü'"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ])
        cursor.execute("UPDATE seals SET active = 1 WHERE active IS NULL")
        cursor.execute("UPDATE seals SET label = 'Kasa Mührü' WHERE label IS NULL OR label = ''")

        # 5. Personel Ödül Kartları Tablosu (Token hash'li saklanır)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS staff_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
        """)
        _ensure_columns(cursor, "staff_cards", [
            ("token_hash", "TEXT"),
            ("label", "TEXT NOT NULL DEFAULT 'Personel Kartı'"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ])
        cursor.execute("UPDATE staff_cards SET active = 1 WHERE active IS NULL")

        # 6. Damga Talepleri (Claims)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seal_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT NULL,
                FOREIGN KEY (seal_id) REFERENCES seals (id)
            );
        """)
        _ensure_columns(cursor, "claims", [
            ("seal_id", "INTEGER NOT NULL DEFAULT 1"),
            ("token", "TEXT"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("expires_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("used_at", "TEXT NULL"),
        ])

        # 7. Damgalar Tablosu
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stamps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                seal_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                voided_at TEXT NULL,
                voided_reason TEXT NULL,
                reward_id INTEGER NULL,
                FOREIGN KEY (customer_id) REFERENCES customers (id),
                FOREIGN KEY (seal_id) REFERENCES seals (id),
                FOREIGN KEY (reward_id) REFERENCES rewards (id)
            );
        """)
        _ensure_columns(cursor, "stamps", [
            ("customer_id", "INTEGER NOT NULL DEFAULT 0"),
            ("seal_id", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("voided_at", "TEXT NULL"),
            ("voided_reason", "TEXT NULL"),
            ("reward_id", "INTEGER NULL"),
            # Rapor için: damganın kaynağı (qr | welcome | bonus_pwa | bonus_notif | staff | admin | campaign) ve işlemi yapan kullanıcı adı
            ("source", "TEXT NULL"),
            ("actor", "TEXT NULL"),
            # Ödüllü kampanya damgasında kampanya adı (kampanya sonradan silinse de geçmişte adıyla görünür)
            ("note", "TEXT NULL"),
        ])
        cursor.execute("UPDATE stamps SET seal_id = 1 WHERE seal_id IS NULL")

        # 8. Ödül Biletleri Tablosu
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                issued_at TEXT NOT NULL,
                redeemed_at TEXT NULL,
                staff_card_id INTEGER NULL,
                voided_at TEXT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers (id),
                FOREIGN KEY (staff_card_id) REFERENCES staff_cards (id)
            );
        """)
        _ensure_columns(cursor, "rewards", [
            ("customer_id", "INTEGER NOT NULL DEFAULT 0"),
            ("issued_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("redeemed_at", "TEXT NULL"),
            ("staff_card_id", "INTEGER NULL"),
            ("voided_at", "TEXT NULL"),
            ("redeemed_by", "TEXT NULL"),  # hediyeyi kullandıran kullanıcı adı (rapor)
        ])

        # 9. Denetim İzi (Audit Log)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL
            );
        """)
        _ensure_columns(cursor, "audit_log", [
            ("at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("actor", "TEXT NOT NULL DEFAULT 'system'"),
            ("action", "TEXT NOT NULL DEFAULT ''"),
            ("detail", "TEXT NOT NULL DEFAULT ''"),
        ])

        # 10. Web Push Abonelikleri Tablosu
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NULL,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                token_hash TEXT NULL,
                created_at TEXT NOT NULL,
                last_success_at TEXT NULL,
                fail_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE SET NULL
            );
        """)
        _ensure_columns(cursor, "push_subscriptions", [
            ("customer_id", "INTEGER NULL"),
            ("endpoint", "TEXT"),
            ("p256dh", "TEXT NOT NULL DEFAULT ''"),
            ("auth", "TEXT NOT NULL DEFAULT ''"),
            ("token_hash", "TEXT NULL"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            ("last_success_at", "TEXT NULL"),
            ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
        ])

        # 11. Bildirim Gönderim Günlüğü (Notification Log)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                audience TEXT NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        _ensure_columns(cursor, "notification_log", [
            ("kind", "TEXT NOT NULL DEFAULT 'campaign'"),
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("audience", "TEXT NOT NULL DEFAULT 'all'"),
            ("sent_count", "INTEGER NOT NULL DEFAULT 0"),
            ("failed_count", "INTEGER NOT NULL DEFAULT 0"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
        ])

        # 12. Ana Sayfa Kampanyalar & Duyurular Tablosu (campaigns)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                image_url TEXT NULL,
                badge TEXT NULL DEFAULT 'Kampanya',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
        """)
        _ensure_columns(cursor, "campaigns", [
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("image_url", "TEXT NULL"),
            ("badge", "TEXT NULL DEFAULT 'Kampanya'"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            # Ödüllü görev (ADR-26): 0 = düz duyuru; 1-5 = görev doğrulanınca verilecek damga
            ("reward_stamps", "INTEGER NOT NULL DEFAULT 0"),
            ("action_url", "TEXT NULL"),     # görev bağlantısı (ör. Google yorum linki)
            ("action_label", "TEXT NULL"),   # görev düğmesi metni
        ])

        # 12b. Ödüllü kampanya katılımcıları: joined → pending (müşteri "yaptım" dedi) → approved (damga verildi) | rejected
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS campaign_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'joined',
                proof_name TEXT NULL,
                joined_at TEXT NOT NULL,
                submitted_at TEXT NULL,
                reviewed_at TEXT NULL,
                reviewed_by TEXT NULL,
                reject_reason TEXT NULL,
                stamps_awarded INTEGER NOT NULL DEFAULT 0,
                phone_hash TEXT NULL,
                UNIQUE (campaign_id, customer_id)
            );
            CREATE INDEX IF NOT EXISTS idx_campaign_participants_status ON campaign_participants (campaign_id, status);
            CREATE INDEX IF NOT EXISTS idx_campaign_participants_customer ON campaign_participants (customer_id);
        """)

        # 13. Menü / Ürünler (yalnızca bilgilendirme amaçlı; online ödeme yoktur)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT 'Genel',
                image_url TEXT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        _ensure_columns(cursor, "products", [
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("price", "REAL NOT NULL DEFAULT 0"),
            ("category", "TEXT NOT NULL DEFAULT 'Genel'"),
            ("image_url", "TEXT NULL"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
            ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
            ("updated_at", f"TEXT NOT NULL DEFAULT '{now_iso}'"),
            # "✨ Yeni Ürün" etiketi ve indirim/kampanya etiketi (eski fiyat üstü çizili, indirimli fiyat yanda)
            ("is_new", "INTEGER NOT NULL DEFAULT 0"),
            ("discount_price", "REAL NULL"),
            ("discount_label", "TEXT NOT NULL DEFAULT ''"),
        ])

        # 14. Güvenlik olay günlüğü (yetkisiz erişim, hız sınırı aşımı, toplu veri işlemleri...)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                ip TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            );
        """)

        # 15. Güvenlik olayları / veri ihlali şüphesi kayıtları (72 saatlik bildirim süreci için)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS security_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                last_event_at TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'open',
                source_ip TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                event_count INTEGER NOT NULL DEFAULT 1,
                affected_estimate INTEGER NOT NULL DEFAULT 0,
                affected_confirmed INTEGER NULL,
                data_exposed INTEGER NOT NULL DEFAULT 0,
                is_manual INTEGER NOT NULL DEFAULT 0,
                authority_notified_at TEXT NULL,
                subjects_notified_at TEXT NULL,
                closed_at TEXT NULL,
                notes TEXT NOT NULL DEFAULT ''
            );
        """)

        # 16. Çalışan (kasiyer) işlem günlüğü
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS staff_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                staff_name TEXT NOT NULL,
                action TEXT NOT NULL,
                customer_id INTEGER NULL,
                customer_phone_masked TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT ''
            );
        """)

        # 17. Silinen hesap kayıt defteri (anti-fraud): hesabını silen kişinin telefon numarası ve cihaz imzası
        # YALNIZCA geri döndürülemez özet (hash) olarak saklanır. Aynı numara/cihazla tekrar kayıt olunursa
        # hesap sıfırdan, bonussuz (0 damga) başlar; hoş geldin / indirme / bildirim hediyesi bir daha verilmez.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deleted_customers_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_type TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                first_deleted_at TEXT NOT NULL,
                last_deleted_at TEXT NOT NULL,
                delete_count INTEGER NOT NULL DEFAULT 1,
                UNIQUE (key_type, key_hash)
            );
        """)

        # 18. Personel hesapları (kasiyer): telefon + parola ile giriş; her işlem kişiye bağlı denetim izine yazılır.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS staff_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone_e164 TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                last_login_at TEXT NULL
            );
        """)
        _ensure_columns(cursor, "staff_audit_logs", [
            ("staff_id", "INTEGER NULL"),
        ])

        # 19. Kategoriler (menü sekmeleri; müdür ekler / sıralar / siler)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        cursor.execute("SELECT COUNT(*) AS c FROM categories")
        if cursor.fetchone()["c"] == 0:
            order = 10
            for cat_name in DEFAULT_CATEGORIES:
                cursor.execute(
                    "INSERT OR IGNORE INTO categories (name, sort_order, created_at) VALUES (?, ?, ?)",
                    (cat_name, order, now_iso),
                )
                order += 10
            # Mevcut ürünlerin kategorileri de listeye alınır (hiçbir ürün kategorisiz kalmasın)
            cursor.execute("SELECT DISTINCT category FROM products WHERE TRIM(COALESCE(category, '')) != ''")
            for prow in cursor.fetchall():
                cursor.execute(
                    "INSERT OR IGNORE INTO categories (name, sort_order, created_at) VALUES (?, ?, ?)",
                    (prow["category"], order, now_iso),
                )
                order += 10

        # 19b. Şüpheli istismar olayları (süresi dolmuş / tekrar kullanılan QR denemeleri; ADR-14). Telefon/cihaz düz metin tutulmaz.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fraud_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                customer_id INTEGER NULL,
                device_hash TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 1
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fraud_events_customer ON fraud_events (customer_id, at);")

        # 19c. Tek kullanımlık Damga QR kodları / açılışlar (ADR-28, ADR-29, ADR-30). Kodun kendisi değil SHA-256 özeti tutulur;
        # hold_* = kayıtlı olmayan müşterinin kayıt formu süresince koda ayrılan tarayıcı (çerez özeti).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stamp_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                staff_id INTEGER NULL,
                actor TEXT NOT NULL DEFAULT '',
                hold_hash TEXT NULL,
                held_until TEXT NULL,
                used_at TEXT NULL,
                used_by_customer_id INTEGER NULL,
                result TEXT NULL,
                origin TEXT NOT NULL DEFAULT 'screen'
            );
        """)
        # ADR-29: 'scan' = sabit QR / NFC okutmasıyla açılan tek kullanımlık açılış, 'screen' = kasa ekranının canlı kodu.
        # ADR-30: 'nfc' = NTAG 424 DNA dokunuşu (kod = "nfc:UID:sayaç"), 'display' = Kasa QR Ekranı (display_id).
        _ensure_columns(cursor, "stamp_codes", [("origin", "TEXT NOT NULL DEFAULT 'screen'"), ("display_id", "INTEGER NULL")])
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stamp_codes_created ON stamp_codes (created_at);")

        # 19d. NTAG 424 DNA etiketleri (ADR-30): her etiketin görülen en büyük dokunuş sayacı. Sayaç geri gidemez; küçük ya
        # da eşit sayaçlı adres (kopya / eski dokunuş) reddedilir. Etiket silinmez (sayaç kaydı kaybolurdu), devre dışı
        # bırakılır.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nfc_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '',
                last_counter INTEGER NOT NULL DEFAULT -1,
                active INTEGER NOT NULL DEFAULT 1,
                taps INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NULL
            );
        """)

        # 19e. Kasa QR Ekranları (ADR-30): eşleştirilmiş tablet / telefon. token_hash = cihaz çerezinin özeti,
        # pair_hash = tek kullanımlık eşleştirme bağlantısının özeti.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS qr_displays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                token_hash TEXT NULL,
                pair_hash TEXT NULL,
                pair_expires_at TEXT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                paired_at TEXT NULL,
                last_seen_at TEXT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qr_displays_token ON qr_displays (token_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_qr_displays_pair ON qr_displays (pair_hash);")

        # 19f. Müşteri ve kasa işlem kayıtları (ADR-31): her damga isteğinin sonucu (verildi / verilmedi + nedeni),
        # kayıt, kasiyer / müdür işlemleri, hediye teslimi. flagged=1 → olası kötüye kullanım; flag_note nedenini sade
        # dille yazar. Müşteri adı / telefonu metne yazılmaz (customer_id ile okunur). ip ve device_hash 1 yıl, kayıt
        # 3 yıl sonra silinir (retention).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS customer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                customer_id INTEGER NULL,
                related_customer_id INTEGER NULL,
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL DEFAULT 'info',
                channel TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                flagged INTEGER NOT NULL DEFAULT 0,
                flag_note TEXT NOT NULL DEFAULT '',
                opened_via TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                stamps_after INTEGER NULL,
                code_id INTEGER NULL,
                ip TEXT NOT NULL DEFAULT '',
                device_hash TEXT NOT NULL DEFAULT ''
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_events_customer ON customer_events (customer_id, id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_events_related ON customer_events (related_customer_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_events_at ON customer_events (at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_events_flagged ON customer_events (flagged, id);")

        # 20. Panel kullanıcıları: müdür (manager) ve kasiyer/personel (staff) kullanıcı adı + parola ile girer.
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'admin_users'")
        admin_users_existed = cursor.fetchone() is not None
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff',
                display_name TEXT NOT NULL DEFAULT '',
                phone_e164 TEXT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                last_login_at TEXT NULL
            );
        """)
        if not admin_users_existed:
            # Eski telefonla girişli personel hesaplarını (kimlik numaraları korunarak) taşı:
            # geçmiş denetim kayıtları aynı staff_id ile eşleşmeye devam eder.
            cursor.execute("SELECT * FROM staff_accounts ORDER BY id ASC")
            for old in cursor.fetchall():
                digits = "".join(ch for ch in (old["phone_e164"] or "") if ch.isdigit())
                legacy_username = digits[-10:] if len(digits) >= 10 else f"personel{old['id']}"
                cursor.execute(
                    "INSERT OR IGNORE INTO admin_users (id, username, password_hash, role, display_name, phone_e164, "
                    "active, created_at, created_by, last_login_at) VALUES (?, ?, ?, 'staff', ?, ?, ?, ?, ?, ?)",
                    (old["id"], legacy_username, old["password_hash"], old["name"], old["phone_e164"],
                     old["active"], old["created_at"], old["created_by"], old["last_login_at"]),
                )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_admin_users_role ON admin_users (role, active);")
        # Çıkış yapılan panel oturumları (G-06): oturum çerezindeki rastgele kimlik (x) süresi dolana kadar geçersiz sayılır.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS revoked_sessions (
                sid TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL
            );
        """)
        # Kampanya / bildirim şablonları (müdür: ekle, düzenle, sil). kind: 'campaign' | 'push'
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS message_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_message_templates_kind ON message_templates (kind, sort_order);
        """)
        # Tek kullanımlık SMS kodları (OTP, docs/OTP.md): kayıtlı numara yeni cihazdan girilince. Kodun kendisi DEĞİL,
        # HMAC-SHA256(SECRET_KEY, id + kod) özeti tutulur; IP de anahtarlı özetle. Saklama görevi süresi geçenleri siler.
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS otp_challenges (
                id TEXT PRIMARY KEY,
                phone_e164 TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                purpose TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                consumed_at TEXT NULL,
                last_sent_at TEXT NULL,
                send_count INTEGER NOT NULL DEFAULT 0,
                ip_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_otp_challenges_phone ON otp_challenges (phone_e164, purpose, created_at);
            CREATE INDEX IF NOT EXISTS idx_otp_challenges_expires ON otp_challenges (expires_at);
        """)
        _ensure_columns(cursor, "otp_challenges", [
            ("last_sent_at", "TEXT NULL"),
            ("send_count", "INTEGER NOT NULL DEFAULT 0"),
            ("ip_hash", "TEXT NOT NULL DEFAULT ''"),
        ])
        _seed_default_accounts(cursor, now_iso)
        _backfill_bonus_proof(cursor)

        # İndeksler
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_type_at ON audit_logs (event_type, at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_ip_at ON audit_logs (ip, at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_incidents_status ON security_incidents (status, detected_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_staff_audit_at ON staff_audit_logs (at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers (phone_e164);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_devices_hash ON devices (token_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_claims_token ON claims (token);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stamps_customer ON stamps (customer_id, voided_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rewards_customer ON rewards (customer_id, redeemed_at, voided_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_push_subs_customer ON push_subscriptions (customer_id, active);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_push_subs_token_hash ON push_subscriptions (token_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_log_audience_created ON notification_log (kind, audience, created_at);")

        # Varsayılan ayarları ekle
        for key, default_value in DEFAULT_SETTINGS.items():
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
                (key, default_value)
            )

        # Hazır şablonlar: yalnızca bir kez tohumlanır (bayrak); müdür silince geri gelmez, düzenleyince ezilmez.
        cursor.execute("SELECT value FROM settings WHERE key = 'message_templates_seeded'")
        if not cursor.fetchone():
            cursor.execute("SELECT value FROM settings WHERE key = 'business_name'")
            brand_row = cursor.fetchone()
            brand = (brand_row["value"] if brand_row else "") or DEFAULT_SETTINGS["business_name"]
            seed_order = {"campaign": 10, "push": 10}
            for kind, label, title, body in default_message_templates(brand):
                cursor.execute(
                    "INSERT INTO message_templates (kind, label, title, body, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, label, title, body, seed_order[kind], now_iso),
                )
                seed_order[kind] += 10
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('message_templates_seeded', '1')")

        # Marka geçişi: eski işletme adı → DEFAULT_BUSINESS_NAME (yönetici başka bir ad verdiyse dokunma)
        legacy_brand = "Kahve Durağı"
        cursor.execute(
            "UPDATE settings SET value = ? WHERE key = 'business_name' "
            "AND (value LIKE 'Kahve Dura%' OR TRIM(value) = '')",
            (DEFAULT_BUSINESS_NAME,),
        )
        cursor.execute(
            "UPDATE settings SET value = REPLACE(value, ?, ?) WHERE value LIKE ?",
            (legacy_brand, DEFAULT_BUSINESS_NAME, f"%{legacy_brand}%"),
        )
        cursor.execute(
            "UPDATE campaigns SET title = REPLACE(title, ?, ?), "
            "description = REPLACE(description, ?, ?)",
            (legacy_brand, DEFAULT_BUSINESS_NAME, legacy_brand, DEFAULT_BUSINESS_NAME),
        )
        # "Hakkımızda" bölümü kaldırıldı: eski alanları temizle
        cursor.execute(
            "DELETE FROM settings WHERE key IN ('about_text','working_hours','address','contact_phone',"
            "'instagram','facebook','website')"
        )

        # Damga limitleri panelden belirlenir ve açılışta ASLA geri alınmaz (ADR-29). Eskiden burada her açılışta
        # günlük limit 1 → 2 ve bekleme süresi → 0 yapılıyordu; müdürün panelde değiştirdiği değer yeniden başlatmada
        # siliniyordu. Yalnızca bir kez: bekleme kapalıysa (0) varsayılan "aynı ziyarette tek QR damgası" 60 dk açılır.
        cursor.execute("SELECT 1 FROM settings WHERE key = 'qr_cooldown_default_v1'")
        if not cursor.fetchone():
            cursor.execute("UPDATE settings SET value = '60' WHERE key = 'min_interval_minutes' AND value = '0'")
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('qr_cooldown_default_v1', '1')")

        # ADR-36 (Y-02): "Bildirimleri Aç" ile paketli verilmiş eski kampanya onayları BİR KEZ geçersiz sayılır ve yeniden onaya alınır.
        cursor.execute("SELECT 1 FROM settings WHERE key = 'bundled_consent_reconfirm_v1'")
        if not cursor.fetchone():
            result = reconfirm_bundled_marketing_consents(conn)
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('bundled_consent_reconfirm_v1', ?)",
                (json.dumps(result),),
            )
            if result["reconfirm"]:
                log_audit(conn, "system", "marketing_consent_reconfirm_migration",
                          f"Paketli kampanya onayı geçişi: {result} (ADR-36).")

        # Eski (sürümsüz) KVKK metnini yeni aydınlatma metniyle değiştir; yönetici özelleştirdiyse dokunma.
        cursor.execute("SELECT value FROM settings WHERE key = 'kvkk_text'")
        kvkk_row = cursor.fetchone()
        if kvkk_row:
            current_kvkk = kvkk_row["value"] or ""
            is_legacy_default = (
                "kişisel verilerinizin güvenliğine önem veriyoruz" in current_kvkk
                and "AYDINLATMA METNİ" not in current_kvkk
            )
            # Özelleştirilmemiş eski varsayılan (v1/v2) → güncel sürüm; sürüm de güncellenir ki yeni teyitler hangi
            # metne verildiği bilinerek kaydedilsin.
            old_defaults = (_KVKK_TEXT_V1.strip(), KVKK_TEXT_V2.strip(), KVKK_TEXT_V3.strip(), KVKK_TEXT_V4.strip(),
                            KVKK_TEXT_V5.strip(), KVKK_TEXT_V6.strip())
            if is_legacy_default or not current_kvkk.strip() or current_kvkk.strip() in old_defaults:
                cursor.execute(
                    "UPDATE settings SET value = ? WHERE key = 'kvkk_text'",
                    (DEFAULT_SETTINGS["kvkk_text"],)
                )
                cursor.execute(
                    "UPDATE settings SET value = ? WHERE key = 'kvkk_version'", (DEFAULT_SETTINGS["kvkk_version"],)
                )

        # Açık rıza / ticari ileti onay metni: özelleştirilmemiş eski varsayılan (v1/v2/v3) → güncel sürüm; sürüm de
        # güncellenir (v3 → v4, ADR-27: bildirimdeki "Bildirimleri kapat" düğmesi metinden çıktı).
        cursor.execute("SELECT value FROM settings WHERE key = 'marketing_consent_text'")
        mkt_row = cursor.fetchone()
        if mkt_row and (mkt_row["value"] or "").strip() in (
            "", _MARKETING_CONSENT_TEXT_V1.strip(), _MARKETING_CONSENT_TEXT_V2.strip(), _MARKETING_CONSENT_TEXT_V3.strip()
        ):
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = 'marketing_consent_text'",
                (DEFAULT_SETTINGS["marketing_consent_text"],)
            )
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = 'marketing_consent_version'",
                (DEFAULT_SETTINGS["marketing_consent_version"],)
            )

        # ADR-21: dinamik/süreli QR ve düzenlenebilir çağrı metinleri kaldırıldı (dinamik QR artık her zaman kapalı)
        cursor.execute(
            f"DELETE FROM settings WHERE key IN ({','.join('?' for _ in REMOVED_SETTING_KEYS)})",
            REMOVED_SETTING_KEYS,
        )

        # Yurt dışı sunuculara (Unsplash vb.) bağlanan eski varsayılan kampanya görsellerini temizle
        cursor.execute(
            "UPDATE campaigns SET image_url = NULL WHERE image_url LIKE 'http://%' OR image_url LIKE 'https://%'"
        )

        # Sabit QR mührünün ('qr') ve (isteğe bağlı .env STAMP_SEAL_TOKEN) mührünün her zaman aktif olmasını sağla
        cursor.execute(
            "INSERT OR IGNORE INTO seals (token, label, active) VALUES (?, ?, 1)",
            ("qr", "Sabit Kasa QR Mührü")
        )
        cursor.execute("UPDATE seals SET active = 1 WHERE token = 'qr'")
        import os
        custom_stamp_seal = os.getenv("STAMP_SEAL_TOKEN", "").strip()
        if custom_stamp_seal and custom_stamp_seal not in ("qr", REGISTER_SEAL_TOKEN):
            cursor.execute(
                "INSERT OR IGNORE INTO seals (token, label, active) VALUES (?, ?, 1)",
                (custom_stamp_seal, "Kasa Mührü")
            )
        # ADR-35: kayıt / uygulama QR'ı mührü (/m/kayit) — eklemeli ve idempotent; her açılışta etkin kalır
        cursor.execute(
            "INSERT OR IGNORE INTO seals (token, label, active) VALUES (?, ?, 1)",
            (REGISTER_SEAL_TOKEN, "Kayıt / Uygulama QR Mührü (damga vermez)")
        )
        cursor.execute("UPDATE seals SET active = 1 WHERE token = ?", (REGISTER_SEAL_TOKEN,))

        # Örnek menü: yalnızca bir kez tohumlanır (sonraki panel değişiklikleri korunur).
        # Testlerde (ve MENU_SEED=0 iken) atlanır.
        if os.getenv("MENU_SEED", "1") != "0":
            from app.menu_seed import seed_menu_once
            seed_menu_once(conn)

        # Ürünlerde kullanılan ama kategori listesinde olmayan kategoriler (menü tohumu, eski veri) listeye eklenir.
        cursor.execute("SELECT COALESCE(MAX(sort_order), 0) AS m FROM categories")
        next_order = cursor.fetchone()["m"] + 10
        cursor.execute(
            "SELECT DISTINCT TRIM(category) AS name FROM products WHERE TRIM(COALESCE(category, '')) != '' "
            "AND LOWER(TRIM(category)) NOT IN (SELECT LOWER(name) FROM categories) ORDER BY name COLLATE NOCASE"
        )
        for missing in cursor.fetchall():
            cursor.execute(
                "INSERT OR IGNORE INTO categories (name, sort_order, created_at) VALUES (?, ?, ?)",
                (missing["name"], next_order, now_iso),
            )
            next_order += 10

        # Kampanyalar tablosu boşsa varsayılan kampanyaları ekle
        cursor.execute("SELECT COUNT(*) as cnt FROM campaigns")
        if cursor.fetchone()["cnt"] == 0:
            cursor.execute(
                """
                INSERT INTO campaigns (title, description, image_url, badge, active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    "Pistachio Latte & Kruvasan",
                    "Organik Antep fıstığı kreması ve yulaf sütüyle hazırlanan imza latte'mizde bu haftaya özel çift damga fırsatı.",
                    None,
                    "Sezonluk",
                    now_iso,
                ),
            )
            cursor.execute(
                """
                INSERT INTO campaigns (title, description, image_url, badge, active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    "Kendi Termosunu Getir",
                    "Kendi kupa veya termosunuzla yapacağınız tüm filtre kahve ve espresso bazlı içeceklerde anında sadakat damgası!",
                    None,
                    "Doğa Dostu",
                    now_iso,
                ),
            )

        # ADR-26: hazır "Google'da yorum" ödüllü kampanyası bir kez, PASİF olarak eklenir. Müdür Google yorum
        # bağlantısını ekleyip "Yayınla" der; silerse geri gelmez.
        cursor.execute("SELECT value FROM settings WHERE key = 'reward_campaign_seeded_v1'")
        if not cursor.fetchone():
            cursor.execute("SELECT value FROM settings WHERE key = 'business_name'")
            brand_row = cursor.fetchone()
            brand = (brand_row["value"] if brand_row else "") or DEFAULT_SETTINGS["business_name"]
            title, description, badge, label = default_reward_campaign(brand)
            cursor.execute(
                """
                INSERT INTO campaigns (title, description, image_url, badge, active, created_at, reward_stamps, action_url, action_label)
                VALUES (?, ?, NULL, ?, 0, ?, 1, NULL, ?)
                """,
                (title, description, badge, now_iso, label),
            )
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reward_campaign_seeded_v1', '1')")

        # ADR-31: mevcut kayıt / damga / hediye / şüphe kayıtları işlem listesine yalnızca bir kez aktarılır (bayrak).
        # Aktarım hatası uygulamanın açılmasını ASLA engellemez (bayrak yazılmaz, sonraki açılışta yeniden denenir).
        from app.services.activity import backfill_once
        cursor.execute("SAVEPOINT customer_events_backfill")
        try:
            backfill_once(conn)
            cursor.execute("RELEASE SAVEPOINT customer_events_backfill")
        except Exception as exc:  # pragma: no cover - savunma
            cursor.execute("ROLLBACK TO SAVEPOINT customer_events_backfill")
            cursor.execute("RELEASE SAVEPOINT customer_events_backfill")
            logging.getLogger("sadakat").warning("İşlem kayıtları geçmiş aktarımı atlandı: %s", exc)


def get_all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Mevcut tüm ayarları sözlük olarak döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    settings = dict(DEFAULT_SETTINGS)
    for row in rows:
        settings[row["key"]] = row["value"]
    return settings


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Tek bir ayar değerini döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    if row:
        return row["value"]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Ayar değerini günceller veya ekler."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value))
    )


def get_kvkk_text(settings: dict[str, str]) -> str:
    """Aydınlatma metnine (varsa) veri sorumlusunun unvan/adresini ve başvuru kanalını ekler (KVKK m.10/a, m.13)."""
    text = settings.get("kvkk_text", "") or ""
    controller = (settings.get("kvkk_controller", "") or "").strip()
    address = (settings.get("kvkk_address", "") or "").strip()
    contact = (settings.get("kvkk_contact", "") or "").strip()
    if controller:
        text = f"{text}\n\nVeri Sorumlusu (unvan ve adres): {controller}"
        if address:
            text = f"{text}\nAdres: {address}"
    elif address:
        text = f"{text}\n\nVeri Sorumlusu (unvan ve adres): {address}"
    if contact:
        text = f"{text}\n\nBaşvuru kanalı: {contact}"
    version = (settings.get("kvkk_version", "") or "").strip()
    if version:
        text = f"{text}\n\nMetin sürümü: {version}"
    return text


def get_marketing_consent_text(settings: dict[str, str]) -> str:
    """'Açık Rıza ve Ticari Elektronik İleti Onay Metni' (sürümüyle; aydınlatmadan ayrı sunulur)."""
    text = settings.get("marketing_consent_text", "") or DEFAULT_SETTINGS["marketing_consent_text"]
    version = (settings.get("marketing_consent_version", "") or "").strip()
    return f"{text}\n\nMetin sürümü: {version}" if version else text


def log_audit(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
    """Denetim günlüğüne kayıt ekler."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (get_istanbul_now_iso(), actor, action, detail)
    )
