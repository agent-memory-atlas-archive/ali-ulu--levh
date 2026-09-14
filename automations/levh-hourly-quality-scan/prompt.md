# Saat başı LEVH kalite taraması — test + lint + ölü kod + açık issue takibi

Klonlanmış `ali-ulu/levh` reposunda (main) her saat başı ÇALIŞTIRMA yap ve sonucu GitHub'da TEK bir issue'da güncel tut. Amaç: geliştiricinin kaçırdığı / tam çözemediği / yeni eklenen sorunları yakalamak.

## ADIMLAR

1. **Test** (tam takım, CI ile aynı): `EMBEDDER_MODE=hash python -m pytest -q` çalıştır, tamamen bitmesini bekle. Son satırı (passed/failed/skipped sayısı) ve varsa hata detaylarını (fail olan test dosyası + test adı) topla.
2. **Lint**: `python -m ruff check .` çalıştır. Varsa ihlallerin kod+dosya+satır listesini topla.
3. **Ölü kod / boş fonksiyon taraması**: Aşağıdaki komut dosyasını çalıştır ve çıktıyı topla:
   - Tanımlı ama hiç çağrılmayan module-level fonksiyonlar (dekoratörsüz) — FastAPI route'larını yanlış ölü sayma (dekoratörlü olanları atla).
   - `pass` gövdesi olan fonksiyon/metodlar.
   - `TODO/FIXME/HACK` yorumları (server/ içinde).
4. **Açık issue'ları izle**: `gh issue list --repo ali-ulu/levh --state open --json number,title,labels` (token: GITHUB_PERSONAL_ACCESS_TOKEN yoksa GITHUB_TOKEN, gh için GH_TOKEN). Açık quality-issue sayısını ve başlıklarını topla. Bir önceki çalıştırmadan beri KAPATILMIŞ issue varsa onu da not et (repo'ya ne zaman düzeltme girdiğini anlamak için).
5. **SORUNLARI AYRI ISSUE OLARAK AÇ** (ana değişiklik): Taramada bulduğun HER yeni/doğrulanmış sorunu "Hourly Quality Scan" özet issue'sunun içine gömmek yerine **kendi GitHub issue'su olarak aç**. 
   - Başlık formatı: `[quality-scan] <kısa açıklama>` (ör. `[quality-scan] server/cli_parsers.py: ölü kod`, `[quality-scan] ruff E501 ihlalleri`).
   - Önce tekrarı önle: `gh issue list --repo ali-ulu/levh --search "[quality-scan]" --state open --json number,title` ile açık `[quality-scan]` issue'larını listele. Bulguyla aynı konuda AÇIK bir issue zaten varsa YENİSİNİ AÇMA; o issue'yu bul, varsa gövdesine "son tarama notu" ekle (tarihli tek satır). Konu farklıysa yeni issue aç.
   - Issue gövdesi Türkçe ve kompakt:
     - Ne bulundu (test hatası / lint ihlali / ölü kod / pas gövdesi / TODO / mantık sorunu)
     - Dosya:satır referansları
     - Önem: P0 (kritik/kırık) / P1 (borç) / P2 (önemsiz)
     - Nasıl çözüleceğine dair 1-2 cümlelik öneri
   - Label: repo'da `quality-scan` label'ı varsa ekle; yoksa labels'sız aç (label'ı otomatik yaratma — repo sahibi ister).
   - `[quality-scan]` ön eki sayesinde kullanıcı bunları kolay süzer ve PR/commit ile çözünce `#` bağlantısı kurar. Bulduğun sorunu KENDİN çözme — sadece raporla/aç.
6. **Özet issue'yu güncel tut** (hâlâ tek "Hourly Quality Scan" issue'su): 
   - `gh issue list --repo ali-ulu/levh --search "Hourly Quality Scan" --state all --json number,state` ile bul. Yoksa oluştur; varsa gövdesini güncelle; kapalıysa reopen et. Başlığı ASLA değiştirme, yorumlara asla yazma.
   - Özet gövdesine bu saatte AÇTIĞIN ve HÂLÂ AÇIK olan `[quality-scan]` issue'larının numara-başlık listesini koy (detayları tekrarlama — sadece link listesi). Böylece tek issue'ya gömmek yerine her sorun takip edilebilir olur.

## RAPOR FORMATI (Türkçe, kompakt)

''' (özet issue gövdesi — başlık "Hourly Quality Scan")
🕐 LEVH Kalite Taraması — <UTC ISO saat>

✅ TEST: 943 passed, 1 skipped, 0 failed (süre ~40s)
🔍 LINT: ruff temiz / N ihlal
🧹 ÖLÜ KOD: N yeni aday (listele: dosya:satır fonksiyon)
📌 AÇILAN ISSUE: N yeni [quality-scan] issue (numara #X, #Y ... başlık listesi)
📋 AÇIK KALAN: N önceki [quality-scan] issue hâlâ açık

KRİTİK: (varsa) test kırılması veya en önemli 3 bulgu
NOT: Otomatik üretildi — saat başı yenilenir.
'''

Her `[quality-scan]` issue'su kendi gövdesinde tam detay taşır (dosya:satır,
önem, öneri). Özet issue SADECE listeler ve saat başı yenilenir.

- Test KIRMIZI ise KRİTİK bölümünü vurgula, fail olan test adlarını ekle.
- Token yoksa durumu "RAPORLANAMADI (token)" olarak işaretle.
- İnternet yoksa test+tarama yine yap, raporu "RAPORLANAMADI (offline)" olarak işaretle.
- Görev kapsamı dışında koda DOKUNMA — bu bir tarama/bekçi otomasyonudur, düzeltme yapmaz. Yeni sorunların asıl çözümü geliştiriciye/PR'lara aittir; sen sadece tespit edip raporlarsın.