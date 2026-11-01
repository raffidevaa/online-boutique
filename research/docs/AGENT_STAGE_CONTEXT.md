# Konteks agent antar-tahap

## Masalah sebelum perbaikan

Satu run `full` menjalankan Detection → Localization → Analysis → Recommendation (jika
RCA disubmit) → Mitigation dengan satu fault. Setiap tahap memanggil
`AgentOrchestrator.init_problem()`, yang memanggil `LlmAgent.init_context()`.
`init_context()` mengganti riwayat pesan agent dengan system prompt baru. Karena itu
agent memulai investigasi ulang pada tahap berikutnya, walaupun tahap sebelumnya
sudah membaca telemetry yang sama.

Saat ini hanya Analysis → Recommendation yang secara eksplisit meneruskan
`rca_solution` dan `rca_trace`. Detection → Localization → Analysis dan
Recommendation → Mitigation tidak menerima ringkasan temuan sebelumnya.
Instruksi setiap tugas juga menyarankan triase awalnya sendiri. Pada run
`EXP-20261101T133424853562Z-32efac`, pengulangan ini ikut membuat tahap
Mitigation dimulai setelah fault 120 detik berakhir.

## Hubungan dengan AIOpsLab

AIOpsLab upstream memperlakukan Detection, Localization, Analysis, dan Mitigation
sebagai jenis *problem* yang dapat dijalankan dan dievaluasi secara terpisah.
Orkestrator upstream menginisialisasi satu problem, menyimpan aksi/observasi dalam
satu sesi, lalu mengevaluasi dan menutupnya. Pipeline lima tahap dalam satu fault
adalah adaptasi proyek ini; AIOpsLab tidak mewajibkan reset konteks di antara
tahap-tahap pipeline adaptasi tersebut.

Sumber: [orkestrator AIOpsLab](https://github.com/microsoft/AIOpsLab/blob/main/aiopslab/orchestrator/orchestrator.py)
dan [panduan menambah problem](https://github.com/microsoft/AIOpsLab#how-to-add-new-problems-to-aiopslab).

## Implementasi saat ini

Sesi dan skor tetap terpisah per tugas. Dalam run `full`, setiap tahap setelah
Detection menerima jawaban dan hingga delapan observasi dari setiap tahap terdahulu.
Setiap observasi dibatasi 3.000 karakter agar konteks tidak tumbuh tanpa batas.
Instruksi awal meminta agent melanjutkan dari temuan tersebut dan hanya meminta
telemetry tambahan jika bukti lama kurang atau sudah usang. Ground truth dan
label evaluator tidak diteruskan. Recommendation tetap berupa saran; Mitigation
menerima hasil RCA dan saran sebelumnya sebagai konteks, dengan aksi eksekusi
tetap dibatasi oleh allowlist layanan target.
Jika temuan terdahulu membenarkan pemulihan, Mitigation diarahkan untuk memanggil
`restore_service` sebagai aksi pertama agar fault tidak habis sebelum aksi dimulai.

Masing-masing tahap masih memiliki batas langkah sendiri dan konteks LLM baru.
Konteks yang diteruskan adalah hasil serta observasi agent, bukan seluruh
percakapan. Dengan begitu, langkah yang sama tidak lagi diwajibkan pada awal
setiap tahap, tetapi agent masih dapat mengulang query bila diperlukan.
