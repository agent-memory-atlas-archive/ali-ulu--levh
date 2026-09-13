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
5. **Raporu tek issue'da tut** — başlık tam olarak `"Hourly Quality Scan"`:
   - `gh issue list --repo ali-ulu/levh --search "Hourly Quality Scan" --state all --json number,state` ile bul.
   - Yoksa oluştur (başlığı ASLA değiştirme). Varsa gövdesini GÜNCELLE. Kapalıysa `gh issue reopen` (yeni issue açma).
   - Issue yorumlarına asla yazma — sadece gövde güncel kalır (24/gün yorum olmasın).

## RAPOR FORMATI (Türkçe, kompakt)

```
🕐 LEVH Kalite Taraması — <UTC ISO saat>

✅ TEST: 943 passed, 1 skipped, 0 failed (süre ~40s)
🔍 LINT: ruff temiz / N ihlal
🧹 ÖLÜ KOD: N yeni aday (listele: dosya:satır fonksiyon)
📌 AÇIK ISSUE: N quality/borç issue (başlıkları listele)

KRİTİK: (varsa) test kırılması veya yeni ölü kod veya en önemli 3 bulgu
NOT: Otomatik üretildi — saat başı yenilenir.
```

- Test KIRMIZI ise KRİTİK bölümünü vurgula, fail olan test adlarını ekle.
- Token yoksa durumu "RAPORLANAMADI (token)" olarak işaretle.
- İnternet yoksa test+tarama yine yap, raporu "RAPORLANAMADI (offline)" olarak işaretle.
- Görev kapsamı dışında koda DOKUNMA — bu bir tarama/bekçi otomasyonudur, düzeltme yapmaz. Yeni sorunların asıl çözümü geliştiriciye/PR'lara aittir; sen sadece tespit edip raporlarsın.