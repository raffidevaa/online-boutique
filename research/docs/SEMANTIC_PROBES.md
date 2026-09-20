# Semantic Probe Plan for Code-Level Fault Experiments

Dokumen ini menjelaskan rencana pengembangan semantic probe otomatis untuk lima
code-level faulty image pada Online Boutique. Tujuan utamanya adalah mengganti
observation JSON manual dengan bukti perilaku aplikasi yang dikumpulkan secara
otomatis selama experiment.

Dokumen ini adalah panduan implementasi dan eksekusi. Fault injection belum
dijalankan hanya dengan membaca dokumen ini.

## Status implementasi

- Schema scenario dan automatic probe engine: implemented.
- CLI `--semantic-probe auto|off`: implemented; default CLI adalah `auto`.
- Observation schema version 2 dan validator compatibility: implemented.
- Unit/orchestration test suite: passing.
- Live smoke test terhadap Compose runtime: passed pada `EXP-20260920T025711149209Z-d8a380`;
  baseline dan incident probe berhasil, semantic validation passed, dan baseline image
  berhasil dipulihkan.

### Runtime stabilization dan retry

Setelah faulty image dinyatakan `healthy`, orchestrator menunggu default 5 detik
agar koneksi gRPC dari frontend ke service target selesai melakukan reconnect.
Nilai ini dapat diubah tanpa mengedit kode:

```bash
export RESEARCH_SEMANTIC_PROBE_STABILIZATION_SECONDS=5
```

Flow `currency_display` juga memiliki retry terbatas untuk kegagalan transient.
Default-nya tiga attempt dengan backoff 1, 2, dan 4 detik:

```bash
export RESEARCH_SEMANTIC_PROBE_RETRY_ATTEMPTS=3
export RESEARCH_SEMANTIC_PROBE_RETRY_BACKOFF_SECONDS=1
```

Retry hanya berlaku untuk kondisi dependency sementara, seperti `connection
refused`, gRPC `Unavailable`, dan response HTTP 502/503/504 atau HTTP 500 yang
memuat marker dependency-unavailable. Error bisnis yang stabil tidak diulang.

`POST /cart/checkout` tidak pernah di-replay otomatis karena dapat membuat
order duplikat. Kegagalan checkout dicatat sebagai hasil repetition tersebut.
Setiap retry menyimpan `attempt`, `retryable`, dan `retry_reason` di request
evidence serta event `semantic_probe_retry` di `events.jsonl`.

## 1. Masalah yang ingin diselesaikan

Saat ini `fault run` sudah dapat:

1. mengambil baseline telemetry;
2. memasang faulty image;
3. mengambil incident telemetry;
4. mengembalikan service ke baseline image; dan
5. memvalidasi file yang diberikan melalui `--semantic-observation`.

Namun file observation tersebut masih dapat dibuat secara manual, misalnya:

```json
{
  "baseline_value": 10,
  "incident_value": 11
}
```

Validator hanya melihat nilai yang diberikan. Validator belum mengetahui apakah
nilai tersebut benar-benar diperoleh dari request ke aplikasi.

Semantic probe akan menjalankan flow bisnis deterministik melalui frontend dan
menghasilkan observation secara otomatis. Dengan demikian:

- `loadgenerator` tetap menjadi workload background;
- semantic probe menjadi penguji perilaku bisnis spesifik;
- logs, traces, dan HTTP response menjadi bukti pendukung; dan
- observation tersimpan langsung di folder experiment.

## 2. Batasan desain

Implementasi harus mengikuti batasan berikut:

- Jangan mengubah source upstream di `src/`.
- Semua implementasi baru berada di bawah `research/`.
- Jangan menjalankan lebih dari satu fault dalam satu experiment.
- Probe dijalankan serial, bukan paralel.
- Probe tidak menggantikan `loadgenerator`.
- Probe tidak boleh menjadi workload berintensitas tinggi.
- Experiment lama di `research/experiments/` bersifat append-only.
- `adservice` dan `recommendationservice` tetap disabled pada baseline saat ini.
- Fault runner harus selalu mencoba restore baseline image melalui cleanup path.

Probe menggunakan HTTP frontend agar yang diukur adalah perilaku yang terlihat
oleh user. Probe tidak melakukan direct mutation terhadap container dan tidak
menggunakan command Docker arbitrary.

## 3. Komponen yang akan ditambahkan

### 3.1 Semantic probe engine

Buat modul:

```text
research/generators/semantic_probe.py
```

Modul ini bertanggung jawab untuk:

- HTTP client dengan cookie/session jar;
- timeout dan retry terbatas;
- request flow baseline dan incident;
- parsing harga, order confirmation, dan cart;
- pembuatan observation terstruktur;
- cleanup cart; dan
- error yang dapat dibedakan antara probe failure dan application failure.

Gunakan Python standard library. Jangan menambahkan dependency eksternal hanya
untuk HTTP client atau parsing HTML.

### 3.2 Konfigurasi frontend

Tambahkan konfigurasi frontend URL ke:

```text
research/utils/config.py
```

Default:

```text
http://127.0.0.1:8080
```

Override melalui environment variable:

```bash
export RESEARCH_FRONTEND_URL=http://127.0.0.1:8080
```

Jangan hardcode endpoint frontend di banyak file.

### 3.3 Konfigurasi probe per scenario

Tambahkan blok `semantic_probe` ke setiap scenario code-level.

Contoh untuk currency:

```yaml
semantic_probe:
  type: currency_display
  product_id: 0PUK6V6EV0
  currency: EUR
  repetitions: 3
  timeout_seconds: 10
```

Contoh untuk checkout:

```yaml
semantic_probe:
  type: checkout_flow
  product_id: 0PUK6V6EV0
  quantity: 1
  repetitions: 3
  timeout_seconds: 15
```

Nilai harus deterministic. Jangan menggunakan random product, random quantity,
atau random currency dalam semantic probe.

## 4. Flow semantic probe

### 4.1 FI-CODE-RETURN-01

Target: `currencyservice`

Flow:

1. Buat session HTTP baru.
2. Kirim `POST /setCurrency` dengan currency tetap, misalnya `EUR`.
3. Buka `/product/0PUK6V6EV0`.
4. Parse nilai pada elemen `.product-price`.
5. Ulangi flow yang sama saat faulty image aktif.
6. Bandingkan nilai baseline dan incident.

Observation minimum:

```json
{
  "currency": "EUR",
  "product_id": "0PUK6V6EV0",
  "baseline_value": "10.00",
  "incident_value": "11.00",
  "baseline_http_status": 200,
  "incident_http_status": 200
}
```

Kriteria lulus:

- request baseline dan incident berhasil;
- product ID sama;
- currency sama;
- nilai incident berbeda dari baseline.

HTTP 200 saja tidak cukup untuk membuktikan fault ini karena fault berada pada
nilai bisnis, bukan pada transport.

### 4.2 FI-CODE-EXC-01

Target: `checkoutservice`

Flow:

1. Buat session baru.
2. Tambahkan satu product ke cart.
3. Jalankan checkout dengan payload fixed yang valid.
4. Ulangi sebanyak tiga kali atau sesuai konfigurasi scenario.
5. Catat HTTP status dan response marker.
6. Korelasikan timestamp request dengan logs/traces `checkoutservice`.

Observation minimum:

```json
{
  "baseline_error_count": 0,
  "incident_error_count": 3,
  "unhandled_exception": true,
  "evidence_source": [
    "frontend_http",
    "checkoutservice_logs",
    "checkoutservice_traces"
  ]
}
```

Kriteria lulus:

- error incident lebih tinggi daripada baseline;
- terdapat bukti `panic`, unhandled exception, atau equivalent failed span;
- error tersebut berasal dari checkout flow, bukan dari probe setup.

HTTP 500 tanpa bukti telemetry tidak otomatis dikategorikan sebagai missing
exception handler.

### 4.3 FI-CODE-PARAM-01

Target: `checkoutservice`

Flow sama dengan checkout probe:

1. Buat session baru.
2. Tambah satu item.
3. Jalankan checkout.
4. Ambil HTTP status, error response, logs, dan traces.

Observation minimum:

```json
{
  "checkout_succeeded": false,
  "incident_error_count": 3,
  "downstream_status": "ERROR",
  "downstream_service": "currencyservice",
  "evidence_source": [
    "frontend_http",
    "checkoutservice_traces",
    "currencyservice_logs"
  ]
}
```

Probe membuktikan dampak runtime, yaitu checkout gagal karena downstream
currency conversion. Probe tidak boleh mengklaim bahwa payload internal
bernilai `INVALID` sebagai nilai yang terobservasi jika payload tersebut tidak
tersedia pada logs/traces.

Identitas mutation `incorrect_parameter` tetap berasal dari faulty-image plan
dan ground truth experiment.

### 4.4 FI-CODE-PARAM-02

Target: `checkoutservice`

Flow sama dengan `FI-CODE-PARAM-01`, tetapi scenario identity tetap
`missing_parameter`.

Kriteria lulus:

- checkout gagal pada incident;
- downstream currency conversion menghasilkan error;
- logs/traces menunjukkan request path yang gagal;
- baseline checkout flow berhasil.

Perbedaan antara `incorrect_parameter` dan `missing_parameter` berasal dari
faulty-image mutation yang dipasang. Semantic probe membuktikan efek aplikasinya,
bukan menebak payload internal yang tidak diekspos oleh service.

### 4.5 FI-CODE-CALL-01

Target: `checkoutservice`

Flow:

1. Buat session baru.
2. Tambahkan satu item ke cart.
3. Jalankan checkout.
4. Pastikan halaman order confirmation berhasil.
5. Buka `/cart` menggunakan session yang sama.
6. Hitung jumlah item setelah checkout.
7. Bersihkan cart setelah probe selesai.

Baseline yang diharapkan:

```json
{
  "checkout_succeeded": true,
  "cart_items_after_checkout": 0
}
```

Incident yang membuktikan fault:

```json
{
  "checkout_succeeded": true,
  "cart_items_after_checkout": 1
}
```

Kriteria lulus:

- checkout tetap berhasil;
- baseline mengosongkan cart;
- incident meninggalkan item di cart.

Fault ini tidak cukup dibuktikan hanya dengan HTTP status karena checkout dapat
tetap mengembalikan HTTP 200.

## 5. Observation schema

File utama yang dibuat otomatis:

```text
research/experiments/runs/<experiment_id>/semantic-observation.json
```

Gunakan schema version 2:

```json
{
  "schema_version": 2,
  "scenario_id": "FI-CODE-CALL-01",
  "probe_type": "checkout_flow",
  "started_at": "2026-09-20T00:00:00Z",
  "baseline": {
    "requests": [],
    "normalized": {}
  },
  "incident": {
    "requests": [],
    "normalized": {}
  },
  "normalized_observation": {},
  "evidence_source": [
    "frontend_http",
    "loki",
    "jaeger"
  ]
}
```

Simpan data request mentah secukupnya untuk audit:

- method dan path;
- timestamp;
- HTTP status;
- latency;
- response marker yang relevan;
- error category;
- jangan menyimpan credit card sensitif atau payload rahasia.

`semantic-validation.json` tetap menjadi hasil validator, sedangkan
`semantic-observation.json` menjadi bukti yang diberikan probe.

## 6. Integrasi ke fault runner

Lifecycle baru pada `research/orchestrator/faults.py`:

```text
preflight
  -> baseline telemetry
  -> baseline semantic probe
  -> apply faulty image
  -> wait target healthy
  -> incident semantic probe
  -> incident telemetry
  -> write semantic observation
  -> validate semantic observation
  -> restore baseline image
  -> recovery telemetry
  -> write final result
```

Baseline semantic probe wajib berhasil. Jika baseline probe gagal:

- faulty image tidak dipasang;
- `result.json` berstatus `baseline_semantic_failed`;
- alasan kegagalan dicatat di `events.jsonl`.

Jika incident probe gagal, cleanup tetap wajib berjalan dan hasil akhir menjadi
`semantic_validation_failed`, bukan meninggalkan faulty image aktif.

## 7. Perubahan CLI

Tambahkan mode automatic probe:

```bash
python -m research.orchestrator fault run \
  --scenario research/generators/scenarios/code-level-01-incorrect-return-currency.yaml \
  --execute \
  --semantic-probe auto
```

Mode yang harus tersedia:

```text
--semantic-probe auto
```

Menjalankan probe dan menulis artifact ke run directory.

```text
--semantic-probe off
```

Menjalankan fault tanpa semantic probe. Gunakan hanya untuk debugging atau
pilot khusus.

```text
--semantic-observation PATH
```

Tetap dipertahankan sementara sebagai compatibility mode untuk experiment lama,
tetapi workflow utama tidak lagi menggunakan file tersebut.

Command utama setelah fitur selesai:

```bash
python -m research.orchestrator fault run \
  --scenario research/generators/scenarios/code-level-01-incorrect-return-currency.yaml \
  --execute
```

Tidak perlu lagi membuat file di `research/experiments/tmp/`.

## 8. Penguatan validator

Modifikasi:

```text
research/generators/semantic.py
```

Validator harus:

- menerima observation schema version 2;
- tetap membaca format version 1 untuk compatibility;
- menolak observation yang tidak memiliki baseline/incident evidence;
- tidak menerima sekadar field manual tanpa evidence source;
- membedakan probe failure dari business behavior failure.

Aturan validator:

| Validator | Syarat lulus |
|---|---|
| `currency_return` | Nilai produk incident berbeda dari baseline dengan flow dan currency yang sama |
| `missing_exception` | Error incident meningkat dan terdapat evidence exception dari checkout telemetry |
| `incorrect_parameter` | Checkout gagal dan downstream conversion path menghasilkan error |
| `missing_parameter` | Checkout gagal dan downstream conversion path menghasilkan error |
| `missing_function_call` | Checkout sukses tetapi item cart incident tetap tersisa |

Untuk `incorrect_parameter` dan `missing_parameter`, validator tidak boleh
mengklaim nilai parameter internal sebagai observed jika tidak tersedia di
telemetry.

## 9. Artifact per experiment

Run directory harus berisi:

```text
scenario.json
faulty-image-plan.json
ground_truth.json
baseline/
incident/
recovery/
semantic-probe.json
semantic-observation.json
semantic-validation.json
cleanup-state.json
injector-result.json
result.json
events.jsonl
```

Folder `research/experiments/tmp/` tidak lagi diperlukan untuk eksekusi normal.
File lama di folder tersebut tidak boleh dihapus karena experiment data bersifat
append-only.

## 10. Urutan implementasi

Implementasi dilakukan bertahap:

### Tahap 1 — Contract dan parser

- Tambahkan model konfigurasi `semantic_probe`.
- Tambahkan schema version 2.
- Tambahkan parser harga, cart, dan order confirmation.
- Tambahkan unit test tanpa Docker.

### Tahap 2 — Probe engine

- Implementasikan HTTP session isolation.
- Implementasikan currency flow.
- Implementasikan checkout flow.
- Implementasikan cleanup cart.
- Pastikan tidak ada random behavior.

### Tahap 3 — Telemetry correlation

- Hubungkan timestamp probe dengan captured logs/traces.
- Implementasikan deteksi exception pada checkout.
- Implementasikan deteksi downstream conversion failure.
- Simpan evidence source dan request timestamp.

### Tahap 4 — Orchestrator integration

- Jalankan baseline probe sebelum injection.
- Jalankan incident probe setelah faulty image healthy.
- Tulis observation otomatis ke run directory.
- Pertahankan restore baseline dalam `finally`.
- Tambahkan CLI `--semantic-probe auto|off`.

### Tahap 5 — Validator dan documentation

- Perkuat validator schema version 2.
- Pertahankan compatibility schema version 1.
- Perbarui `FAULT_INJECTION.md` dan `research/README.md`.
- Tambahkan contoh output untuk setiap fault.

## 11. Testing plan

### Unit test

Uji:

- parser `.product-price`;
- parser cart empty dan non-empty;
- parser order confirmation;
- fixed checkout payload;
- cookie/session isolation;
- timeout dan HTTP error;
- semua lima probe;
- schema version 2;
- compatibility schema version 1.

### Orchestration test

Dengan Compose dan observer yang di-mock, pastikan:

- baseline probe dijalankan sebelum fault;
- fault tidak dipasang jika baseline probe gagal;
- incident probe berjalan setelah target healthy;
- observation ditulis ke run directory;
- baseline image direstore saat probe gagal;
- cleanup tetap berjalan pada exception;
- manual observation lama tetap berfungsi.

### Live smoke test

Jalankan satu per satu:

1. baseline semantic probe tanpa fault;
2. `FI-CODE-RETURN-01`;
3. `FI-CODE-CALL-01`;
4. `FI-CODE-EXC-01`;
5. `FI-CODE-PARAM-01`;
6. `FI-CODE-PARAM-02`.

Jangan menjalankan beberapa faulty image secara bersamaan.

## 12. Validation command

Sebelum experiment:

```bash
docker compose -f research/compose/docker-compose.yml config
docker compose -f research/compose/docker-compose.yml ps
python -m unittest discover -s research/tests -p 'test_*.py'
```

Pastikan:

- target core service healthy;
- `loadgenerator` aktif tetapi tetap low-intensity;
- `adservice` dan `recommendationservice` tidak ikut start;
- Prometheus, Loki, dan Jaeger ready;
- host CPU memiliki headroom yang cukup.

Setelah setiap experiment:

```bash
docker compose -f research/compose/docker-compose.yml ps
docker compose -f research/compose/docker-compose.yml images
```

Periksa `cleanup-state.json` dan pastikan target kembali ke baseline image.

Periksa artifact:

```bash
RUN_PATH="research/experiments/runs/<experiment_id>"
cat "$RUN_PATH/result.json"
cat "$RUN_PATH/semantic-observation.json"
cat "$RUN_PATH/semantic-validation.json"
cat "$RUN_PATH/cleanup-state.json"
```

## 13. Acceptance criteria

Implementasi dianggap selesai jika:

1. Kelima code-level faulty image memiliki automatic semantic probe.
2. Status `completed` tidak membutuhkan file observation manual.
3. Observation tersimpan langsung di run directory.
4. Baseline probe berhasil sebelum fault dipasang.
5. Currency fault membuktikan perubahan nominal harga.
6. Exception fault membuktikan error dan unhandled exception.
7. Kedua parameter fault membuktikan checkout/downstream failure.
8. Missing function call membuktikan checkout sukses tetapi cart tidak kosong.
9. Baseline image selalu direstore.
10. `loadgenerator` tetap aktif sebagai background workload.
11. Tidak ada perubahan pada source upstream.
12. Unit dan orchestration test lulus.
13. `docker compose config` lulus.
14. Tidak ada experiment lama yang diubah atau dihapus.

Status scenario hanya boleh dipromosikan menjadi `ready_for_pilot` setelah live
smoke test berhasil.

## 14. Referensi internal dan eksternal

Referensi internal:

- [`ARCHITECTURE.md`](ARCHITECTURE.md)
- [`FAULT_INJECTION.md`](FAULT_INJECTION.md)
- [`FAULT_TAXONOMY.md`](FAULT_TAXONOMY.md)
- [`research/orchestrator/faults.py`](../orchestrator/faults.py)
- [`research/generators/semantic.py`](../generators/semantic.py)
- [`research/generators/faulty_images/manifest.yaml`](../generators/faulty_images/manifest.yaml)

Referensi metodologi:

- [OpenTelemetry Signals](https://opentelemetry.io/docs/concepts/signals/)
- [OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/concepts/semantic-conventions/)
- [RCAEval](https://github.com/phamquiluan/RCAEval)
