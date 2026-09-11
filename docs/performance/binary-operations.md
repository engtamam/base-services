# تقرير: العمليات الثنائية (Binary / Bitwise) — أين تُستخدم لتسريع العمل وتقليل الضغط

> تقرير عام (غير مرتبط بكودنا). الخطوة التالية المتفق عليها: مراجعة الكود وتحديد الأماكن
> المطابقة فعلاً **بقياس، لا بافتراض**. — أُنشئ 2026-09-11.

## ١) اللبنات الأساسية (المعاملات وما تفعله)

| المعامل | الاستخدام النمطي |
|---|---|
| `&` (AND) | **اختبار/قناع**: هل بِت مضبوط؟ `x & mask` |
| `\|` (OR) | **ضبط** بِت: `x \| mask` |
| `^` (XOR) | **قلب** بِت / فرق المجموعات / مبادلة بلا متغيّر مؤقت |
| `~` (NOT) | عكس + `x & ~mask` = **مسح** بِت |
| `<<` `>>` (shift) | إزاحة = **ضرب/قسمة على قوى ٢**، وتعبئة الحقول |

## ٢) الأماكن اللي فعلاً تستفيد (عام)

**أ. مجموعة أعلام/بوليان في عدد صحيح واحد** — صلاحيات، مزايا، حالات.
- العادي: قائمة/Set + لوب فحص. → الثنائي: `held & required == required` (كل الأعلام **بعملية واحدة**).
- يكسب لما المجموعة **مغلقة ومعروفة** وتُفحص كثير (hot path).

**ب. عضوية في كون صغير ثابت** — bitset (مصفوفة بِت).
- بدل `set` بأوبجكتات: بِت واحد لكل عنصر → ذاكرة أقل وفحص أسرع.

**ج. مجموعات كبيرة/متفرّقة + تقاطع/اتحاد (فلترة)** — **Roaring Bitmaps** (bitset مضغوط).
- تُشغّل الفلترة في Elasticsearch/Lucene/Druid/ClickHouse/Spark — تقاطع ملايين المعرّفات بعمليات
  AND/OR أصلية، أسرع بمئات المرات من البدائل. تُستخدم لما عندك "أي عناصر تطابق كل هذه الشروط".

**د. "موجود؟ غالباً / أكيد لا"** — **Bloom Filter** (مصفوفة بِت + دوال hash).
- يمنع القراءة المكلفة من القرص/الشبكة: لو الفلتر قال "لا" فهو **مؤكد لا**. يُستخدم لـ dedup،
  منع تكرار، فحص "هل الاسم مأخوذ"، تفادي lookups باهظة.

**هـ. ضرب/قسمة/باقي على قوى ٢** — إزاحة وقناع.
- `x * 2^k` → `x << k`؛ `x / 2^k` → `x >> k`؛ `x % 2^k` → `x & (2^k - 1)`.

**و. تعبئة عدة حقول صغيرة في عدد واحد** (pack/unpack) — تخزين/نقل مضغوط (تجميع flags+id في مفتاح واحد).

**ز. حِيَل رياضية سريعة:**
- زوجي/فردي: `n & 1`.
- قوة اثنين؟ `n && (n & (n-1)) == 0`.
- أدنى بِت مضبوط: `n & -n`.
- عدد البِتات المضبوطة (popcount) — تعليمة عتاد واحدة في أغلب المعالجات.
- مبادلة قيمتين بلا مؤقت: `a ^= b; b ^= a; a ^= b`.

**ح. ضبط/مسح/قلب/اختبار بِت محدد:**
- set `x |= (1 << i)` · clear `x &= ~(1 << i)` · toggle `x ^= (1 << i)` · test `x & (1 << i)`.

## ٣) تحذيرات صادقة (مهمة قبل ما نطبّق)

- **اللغة تفرق:** في C/Rust/Go وفي محركات الـDB المكسب كبير. في **بايثون** الأعداد boxed
  وarbitrary-precision، و`x in frozenset` أصلاً O(1) بمستوى C — فمكسب البِت لمجرد "العضوية"
  **صغير**؛ الفائدة الحقيقية في **جبر المجموعات دفعة واحدة** و**الضغط/النقل**.
- **دليل قبل التحسين:** ما نحسّن مكان مو عنق زجاجة. اللوبات المرتبطة بـ I/O (استعلام/شبكة) ما
  تستفيد من البِت — علاجها الفهرسة.
- **الكون لازم مغلق:** تعيين bit ثابت لمفاتيح ديناميكية = hardcode (يخالف config-driven).
- **القراءة:** الكود الثنائي أصعب قراءة — يستاهل فقط لو المكسب مقاس وحقيقي.

## الخلاصة

أعلى قيمة عملياً: **(أ) أعلام الصلاحيات/المزايا** كأقنعة، و**(ج) Roaring bitmaps للفلترة الكبيرة**،
و**(د) Bloom filters لتفادي القراءات الباهظة**. الباقي حِيَل موضعية صغيرة.

---

# ملحق: قواعد البيانات — ما ينطبق على ستاكنا فعلاً (2026-09-11)

بحثنا "أسرار تسريع قواعد البيانات"، ثم راجعنا مخطّطنا. الخلاصة الصادقة:

**بنيتنا:** جدول JSONB عام واحد لكل خدمة — `kv(collection, key, value jsonb)` — مع فهرسين يشحنان
مع الأباتر:
- **GIN** (`value jsonb_path_ops`) → يخدم **كل فلاتر المساواة** (`value @> {"tenant":...}`) تلقائياً.
- **btree للترتيب:** `(collection, COALESCE(value->>'created','') DESC, key DESC)` — يطابق الترتيب الافتراضي `created`.

**لا ينطبق على جدول JSONB عام:** partial / covering(INCLUDE) / BRIN / HOT / fillfactor — تحتاج
جداول/أعمدة محدّدة وتكسر تصميم "جدول واحد لكل شي". GIN أصلاً يغطي المساواة.

**الثغرة الحقيقية الوحيدة:** أي `find` يرتّب بحقل **غير `created`** يقع على المسار البطيء
(bitmap + top-N sort، مقيس ٣.٤٤ms مقابل ٠.٩٢ms) لأن فهرس الترتيب الوحيد على `created`. ونطاقات
مثل `period_end <= now` لا يخدمها GIN (تُصفّى في بايثون).

**مرشّحات الترتيب-بغير-created (مسار طلب + مجموعة تكبر):** notification feeds
(`timestamp`/`ts`/`receipt_ts`)، webhook (`received`)، ticket (`updated`/`seq`)، stream (`started`).
الخلفية (backup/erasure/expiry) والإداري (secret_store/runtime_config) نتجاهلها.

**تعارض تسمية:** verification/compliance/identity تخزّن `created_at`، بينما الطبقة العامة على
`created` → ترتيباتها غير مفهرسة، وقوائم "الأحدث" مرتّبة بـ`key` بدل الزمن. **تنبيه:** `created_at`
في compliance = ساعة احتفاظ قانونية، وفي identity = مرساة keyset + عمر مفتاح توقيع — فأي توحيد
اسم يلمس منطق دومين حسّاس، والقيمة الوظيفية صغيرة.

**القرار (2026-09-11):** الأحمال صغيرة (dev/test)، لا شيء **مكسور وظيفياً** — الفرق غير محسوس.
أوقفنا التحسين هنا؛ نرجع له **بقياس profiler** فقط لو كبرت الأحمال. الأدوات المرجعية:
`tools/perf/find_perf_candidates.py`. الرافعات الحقيقية عند الحاجة: **لا تنسخ، خزّن مؤقتاً، اجمع الـ I/O**
— لا bitwise.

## المصادر

- Roaring Bitmaps — compressed bitset: https://roaringbitmap.org/
- RoaringBitmap (Java, used by Spark/Pinot/…): https://github.com/RoaringBitmap/RoaringBitmap
- Bloom Filters — more than a space-efficient hashmap (Ben Boyter): https://boyter.org/posts/bloom-filter/
- AWS: fast space-efficient lookups with Bloom filters: https://aws.amazon.com/blogs/database/implement-fast-space-efficient-lookups-using-bloom-filters-in-amazon-elasticache
- Improve Elasticsearch filtering 10x (Roaring plugin): https://luis-sena.medium.com/improve-elasticsearch-filtering-performance-10x-using-this-plugin-8c6485516c1a
- Mastering Bit Manipulation — key concepts: https://medium.com/@hanxuyang0826/mastering-bit-manipulation-key-concepts-and-practice-leetcode-problems-c0c328cdf164
- Bitmap and Bloom Filter: https://www.cwblogs.com/posts/bit-map/
