# Laporan implementasi action control dan context management

Tanggal: 2026-11-04. Basis checkout: `e0b084b669d4c10355e20472df43f8e9c10aec6d` (berbeda dari commit `68db035fe13a91a0f70411f4baae3841df75371d` dalam spesifikasi). Perubahan dibatasi pada `research/`. Artefak eksperimen lama tidak diubah.

## Baseline sebelum perubahan

- Maksimum 15 langkah **per tahap**, tanpa batas kumulatif insiden. Percobaan parse yang gagal sudah menghabiskan satu langkah.
- Aksi observasi adalah ringkasan insiden, log error, metrics, logs, traces, dan alerts; `submit` serta `restore_service` khusus tugas masing-masing. Parser menggunakan `ast.literal_eval`, bukan `eval`.
- Detail live memakai `Observer.capture()` yang mengambil semua sinyal. Output mentah dipotong pada 8.000 karakter tanpa hitungan item.
- Sesi menyimpan trace, status submit, alasan berhenti, dan jumlah aksi invalid. Handoff lima tahap membawa jawaban serta sampai delapan potongan observasi per tahap.
- Evaluator terpisah menghitung RCA setelah loop. Validitas rekomendasi sudah dinilai otomatis; correctness rekomendasi masih membutuhkan rubrik review manusia.

## Perubahan per berkas

| Berkas | Perubahan |
|---|---|
| `research/utils/config.py` | Konfigurasi switch strategi, batas kumulatif, token, biaya, waktu, cache, stagnasi, dan kompaksi. |
| `research/agent/providers.py` | Output provider API menyertakan token aktual saat tersedia dan metadata model. Provider fake tetap deterministik untuk tes. |
| `research/agent/models.py` | Metrik, catatan terstruktur, dan hasil parsial dalam sesi. |
| `research/agent/strategy.py` | Validasi aksi, sidik jari query, estimasi token, anggaran insiden, dan cache read-only. |
| `research/agent/orchestrator.py` | Loop berbatas, validasi sebelum observer, deduplikasi, pemeriksaan progres, hasil parsial deterministik, catatan, kompaksi, dan evaluasi ground truth setelah loop. Instruksi output JSON diperjelas setelah provider live sekali mengirim markup DSML. |
| `research/observer/telemetry.py` | Pengambilan satu sinyal live dengan batas waktu, label layanan untuk Loki/Prometheus, rentang waktu, limit, dan resolusi. |
| `research/agent/environment.py` | Ringkasan sinyal ber-ID, jumlah item dan status limit; detail hanya melalui ID sesi; data penuh disimpan di lingkungan. Trace dirangking berdasarkan error lalu durasi. Setelah uji live, skor root network mengabaikan edge dengan kurang dari tiga panggilan dan memakai skor sesuai signature. |
| `research/agent/tasks.py` | Aksi detail dan pembaruan hipotesis; correctness rekomendasi dicatat `null` sampai review. |
| `research/agent/live.py` | Anggaran satu insiden untuk semua tahap; handoff berupa catatan, ID bukti, dan usia bukti; metrik tahap masuk artefak evaluasi baru. |
| `research/agent/README.md` | Konfigurasi dan arti metrik. |
| `research/tests/test_agent_strategy.py` | Tes offline untuk batas, cache, validasi, stagnasi, parsial, ID bukti, isolasi ground truth, kompaksi, handoff, dan retrieval fokus. |
| `research/docs/ACTION_CONTROL_AND_CONTEXT_MANAGEMENT_PROMPT.md` | Spesifikasi yang menjadi patokan kerja ini, dibuat pada giliran sebelumnya. |

## Status 13 strategi

| No. | Strategi | Status | Bukti dan batas |
|---:|---|---|---|
| 1 | Batas iterasi | Diterapkan | `max_steps` per tahap dan `AIOPS_INCIDENT_MAX_STEPS` bersama; parse gagal dihitung. |
| 2 | Token dan biaya | Diterapkan | Usage aktual bila provider memberi data, selain itu estimasi berlabel; biaya `null` tanpa tarif. Prediksi biaya pra-panggilan memakai estimasi. |
| 3 | Deduplikasi | Diterapkan | Sidik jari aksi/argumen/run/window/layanan/resolusi; replay immutable, live memakai bucket waktu dan TTL. |
| 4 | Validasi aksi/API | Diterapkan | Nama, parameter, layanan metadata, window, ID bukti, dan limit diperiksa sebelum observer. |
| 5 | Pemeriksaan progres | Diterapkan sebagian | Bukti baru dan perubahan hipotesis dicatat; stagnasi memicu permintaan bukti spesifik lalu berhenti. Penyempitan kandidat belum dihitung sebagai metrik tersendiri. |
| 6 | Definisi selesai | Diterapkan sebagian | Kontrak `submit` lama dipertahankan dan layanan divalidasi. Validitas skema tidak dianggap kebenaran; correctness rekomendasi menunggu review manusia. |
| 7 | Hasil parsial | Diterapkan | Stop karena batas/stagnasi menyimpan hipotesis, ID bukti, klaim belum diverifikasi, dan alasan; tidak melakukan submit tebakan. |
| 8 | Context just-in-time | Diterapkan sebagian | Detail live mengambil satu sinyal; ringkasan awal masih memakai capture penuh dua window untuk agregat. Arsip penuh tetap di luar konteks model. |
| 9 | Disclosure bertahap | Diterapkan | Ringkasan insiden → sinyal layanan → detail menurut ID sesi; tidak menerima path. |
| 10 | Tool hemat token | Diterapkan | Agregat, sampel, jumlah total/ditampilkan, flag limit, query terikat, dan ranking trace. Detail sendiri dibatasi 20 item serta sampel per item. |
| 11 | Catatan terstruktur | Diterapkan | `update_hypothesis` menyimpan status, kandidat, bukti mendukung/menyangkal, pertanyaan berikut, dan waktu; ID harus berasal dari sesi. |
| 12 | Kompaksi | Diterapkan | Rekonstruksi deterministik dari instruksi tetap, catatan, ID bukti, dan pesan terbaru; trace audit penuh tetap pada sesi. Dampak kualitas RCA belum diukur dengan provider API. |
| 13 | Handoff tahap | Diterapkan | `live.py` mengirim temuan dan ID, bukan fragmen observasi atau label evaluator; usia bukti ditandai untuk refresh. |

## Verifikasi

- `.venv/bin/python -m unittest research.tests.test_agent_strategy -q`: 13 tes lulus.
- `.venv/bin/python -m unittest discover -s research/tests -q`: 121 tes lulus setelah perbaikan hasil live.
- `python -m compileall -q research/agent research/observer research/utils`: lulus.
- `git diff --check`: lulus.

Python sistem tidak memiliki `fastapi`, sehingga percobaan suite penuh pertama gagal pada impor `test_web`. Suite dijalankan ulang memakai `.venv` repo. Perbandingan replay memakai provider fake; uji live berikutnya memakai provider API terkonfigurasi.

## Perbandingan replay awal

Replay memakai run tervalidasi `EXP-20261102T130842462933Z-69d7df` (`FI-CODE-RETURN-01`, `incorrect_return_value`), timestamp dan artefak yang sama, `FakeProvider` yang sama, empat aksi tetap (ringkasan, ringkasan berulang, log error layanan, submit), `max_steps=5`, dan tanpa menulis artefak run. Ground truth hanya dibaca saat evaluasi.

| Metrik | Baseline | Strategi aktif |
|---|---:|---:|
| RCA benar / submit valid | Ya / ya | Ya / ya |
| Langkah | 4 | 4 |
| Aksi baca telemetry yang dieksekusi | 3 | 2 |
| Percobaan duplikat | 1 | 1 |
| Cache hit | 0 | 1 |
| Token input (estimasi) | 19.885 | 19.885 |
| Token output (estimasi) | 72 | 72 |
| Karakter observasi | 25.559 | 25.559 |
| Alasan berhenti | submitted | submitted |

Ini membuktikan satu query replay dihindari pada urutan aksi tetap. Tidak ada pengurangan token karena respons cache yang sama tetap diberikan ke model. Switch baseline menonaktifkan kontrol baru, tetapi memakai format ringkasan bukti baru yang sama; jadi perbandingan ini mengisolasi kontrol aksi, bukan membandingkan seluruh sistem lama dengan sistem baru. Waktu eksekusi singkat dan variabel host tidak cukup untuk klaim peningkatan latensi.

`actual_queries` menghitung aksi baca agent yang benar-benar dieksekusi, bukan jumlah request HTTP ke backend. Satu aksi ringkasan dapat memicu beberapa request.

## Uji live setelah stack dinyalakan

`docker compose config -q`, `docker compose up -d`, dan `research.orchestrator doctor` berhasil. `adservice`, Prometheus, Loki, Jaeger, dan layanan aplikasi sehat. Loadgenerator rendah (`USERS=10`, `RATE=1`, CPU 0,5) dinyalakan. Start awal loadgenerator gagal karena referensi network Docker yang usang; hanya container itu yang dibuat ulang, lalu berjalan. Image Pumba sudah tersedia tanpa pull.

Satu skenario `FI-NET-LOSS-02` (`packet_loss` 15% pada `productcatalogservice`) dijalankan **dua kali secara berurutan**, tidak paralel, melalui `research.agent --live-incident --task analysis --execute --max-steps 8`. Provider API terkonfigurasi `deepseek-chat`; respons melaporkan model `deepseek-flash` dan token aktual. Tidak ada ground truth dalam prompt atau trace agent.

| Run baru | Hasil agent | Metrik utama | Pemulihan |
|---|---|---|---|
| `EXP-20261104T124353298786Z-561529` | Parsial, `input_token_budget_exhausted` karena override 15.000 token; 1 aksi invalid akibat markup DSML; tanpa submission. | 3 langkah, 2 aksi baca, 11.090 input dan 95 output token aktual. | `result.json` completed, injector returncode 0, recovery recorded. |
| `EXP-20261104T124748403627Z-8fdccf` | Submission valid tetapi RCA **salah**: `checkoutservice`/`cartservice`, `network_delay`; ground truth `productcatalogservice`, `packet_loss`. Rekomendasi `manual_investigation` valid secara skema, correctness belum direview. | Analysis 6 langkah, 5 aksi baca, 33.367 input dan 133 output token aktual; remediation 1 langkah, 1.142 input dan 344 output token aktual. | `result.json` completed, injector returncode 0, recovery recorded. |

Review trace run kedua menunjukkan satu span checkout dengan satu panggilan mendapat skor network 5 karena `loss_score` dipakai untuk signature `network_delay`. Perbaikan sesudah run mensyaratkan sedikitnya tiga panggilan sebelum edge memengaruhi ranking dan memakai `delay_score` hanya untuk delay. Pada **replay read-only** artefak run kedua setelah patch, `productcatalogservice` menjadi anomali teratas dengan skor 1,466734 dan network-root score 0,949008. Ini memverifikasi perubahan ranking; belum membuktikan agent akan mengirim RCA benar pada uji live berikutnya. Tes regresi sintetis ditambahkan. `doctor` sesudah fault berstatus healthy; loadgenerator dihentikan dan stack Compose utama dibiarkan menyala.

## Batas tersisa

- Kedua run live memverifikasi lifecycle dan recovery, tetapi tidak ada RCA live yang benar. Pengaruh patch ranking terhadap keputusan agent masih perlu diuji pada run berikutnya.
- Replay read-only dengan provider API sesudah patch dicoba, tetapi **ditolak oleh automatic approval review** karena akan mengirim telemetry/trace live ke API LLM eksternal tanpa persetujuan khusus untuk payload dan tujuan itu. Tidak ada replay API lanjutan yang dijalankan; verifikasi pasca-patch dibatasi pada ranking lokal dan tes offline.
- Kebenaran rekomendasi membutuhkan review manusia sesuai rubrik yang sudah ada; `recommendation_valid` hanya memeriksa kontrak dan keselamatan format.
- Ringkasan awal live masih melakukan capture lengkap untuk menghitung agregat lintas sinyal. Optimasi selanjutnya dapat memakai endpoint agregat khusus dan membandingkan kualitas RCA pada run live yang terkendali.
- Estimasi token berdasarkan panjang karakter hanya proksi. Hasil biaya dan penghematan konteks yang kuat memerlukan provider yang melaporkan usage dan tarif model yang dikonfigurasi.

## Referensi desain yang diperiksa

- [Anthropic, *Effective context engineering for AI agents*](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents): ID bukti dan pengambilan konteks saat dibutuhkan.
- [Anthropic, *Writing effective tools for agents*](https://www.anthropic.com/research/writing-tools-for-agents): batas output, filter, dan pesan kesalahan yang membantu agent memperbaiki query.
- [Grafana Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/): parameter `query`, `start`, `end`, dan `limit` pada `query_range`.
- [Prometheus HTTP API](https://prometheus.io/docs/prometheus/3.12/querying/api/): ekspresi, `start`, `end`, dan `step` untuk query rentang.
- [Microsoft AIOpsLab](https://github.com/microsoft/AIOpsLab): kontrak problem dan sesi sebagai acuan konseptual; implementasi ini tetap memakai Docker Compose.
