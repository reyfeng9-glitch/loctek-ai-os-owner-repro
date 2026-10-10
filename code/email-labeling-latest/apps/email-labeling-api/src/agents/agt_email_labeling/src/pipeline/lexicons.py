"""集中维护的多语言词表 + 语言无关的结构化辅助。

设计原则
========
邮件来自不同国家的客户（英 / 德 / 法 / 丹麦 / 匈牙利 / 荷兰 / 西 / 意 / 波兰 / 俄 等）。
若把"逐语言关键词"散落在 cleaner.py / system_filter.py 的正则里，会重复、易漏、难维护。

因此本模块遵循两层策略：

1. **主判定走结构化（与语言无关）**——例如：
   - 引用块：任意以 `>` 开头的行、"标签: 值" 头簇（值里含邮箱 + 日期）。
   - 签名：尾部"联系方式密度"（电话 / 邮箱 / URL / 公司地址）。
   - 已读回执：正文是"邮件头回显"且几乎无业务内容。
   这些不认具体语言，新国家客户进来一样能命中。

2. **小语种词表只做兜底补充**——结构化覆盖不到的纯文本线索
   （如"自动回复""已读""退信"的本地化措辞、结尾问候语）。

维护指南
========
- 新增一门语言时：**只在本文件对应列表里追加该语言的词**，
  不要再去 cleaner / system_filter 里加正则。
- 词一律小写；匹配方会做大小写无关处理。
- 加词后请在 tests 里补一条该语言的样例（见 email_labeling_cleaner_spec / _system_filter_spec）。
"""

# ── 引用归属行动词（"<某人> wrote:" 的各语言版本）──
# 仅作结构化头簇检测的补充：命中"含动词且邻近邮箱/日期"的归属行时折叠引用。
ATTRIBUTION_VERBS = [
    "wrote", "writes",                 # 英
    "schrieb", "schreibt",             # 德
    "skrev",                           # 丹麦 / 挪威 / 瑞典
    "a écrit", "écrit",                # 法
    "escribió", "escribio",            # 西
    "ha scritto", "scrisse",           # 意
    "napisał", "napisal",              # 波兰
    "napsal",                          # 捷克
    "írta", "irta",                    # 匈牙利
    "schreef", "geschreven",           # 荷兰
    "написал", "писал",                # 俄
]

# ── 结尾问候语（签名起始）──
# 结构化"联系方式密度"为主；此表用于补充识别问候语开头的签名块。
SIGNATURE_OPENERS = [
    "best regards", "kind regards", "warm regards", "with regards",
    "regards", "sincerely", "cheers", "best wishes", "yours truly",
    "yours sincerely", "yours faithfully", "thanks and regards",
    "med venlig hilsen",                       # 丹麦
    "mit freundlichen grüßen", "viele grüße", "freundliche grüße",  # 德
    "üdvözlettel", "tisztelettel",             # 匈牙利
    "cordialement", "bien cordialement", "salutations",  # 法
    "saludos", "atentamente", "un saludo",     # 西
    "cordiali saluti", "distinti saluti",      # 意
    "met vriendelijke groet", "vriendelijke groeten",  # 荷兰
    "pozdrawiam",                              # 波兰
    "s pozdravem",                             # 捷克
    "с уважением",                             # 俄
]

# ── 机器邮件标记（system_filter 用）──
# 自动回复（out-of-office / 休假 / 自动应答）
AUTO_REPLY_MARKERS = [
    "out of office", "auto-reply", "auto reply", "automatic reply",
    "autoreply", "vacation responder", "auto responder", "automated response",
    "automatische antwort", "abwesenheitsnotiz", "abwesend",   # 德
    "automatisch antwoord", "afwezigheid",                     # 荷兰
    "réponse automatique", "absence du bureau",                # 法
    "respuesta automática", "fuera de la oficina",             # 西
    "risposta automatica", "fuori sede",                       # 意
    "autosvar", "fraværende",                                  # 丹麦/挪威
    "automatiskt svar", "frånvarande",                         # 瑞典
    "automatikus válasz", "házon kívül", "nem vagyok az",      # 匈牙利
    "automatyczna odpowiedź",                                  # 波兰
    "automatická odpověď",                                     # 捷克
]

# 已读回执（read receipt）——纯噪声
READ_RECEIPT_MARKERS = [
    "read:",            # 英
    "gelesen:",         # 德
    "gelezen:",         # 荷兰
    "lu:", "lu :",      # 法
    "leído:",           # 西
    "letto:",           # 意
    "læst:",            # 丹麦
    "läst:",            # 瑞典
    "lest:",            # 挪威
    "elolvasva:",       # 匈牙利
    "przeczytano:",     # 波兰
    "přečteno:",        # 捷克
    "прочитано:",       # 俄
]

# 已读回执的正文常以"邮件头回显"开头（"您的消息……已读"）
READ_RECEIPT_BODY_OPENERS = [
    "your message",       # 英
    "uw bericht",         # 荷兰
    "ihre nachricht",     # 德
    "votre message",      # 法
    "su mensaje",         # 西
    "il tuo messaggio",   # 意
    "din besked",         # 丹麦
    "ditt meddelande",    # 瑞典
    "üzenetét", "az ön üzenete",  # 匈牙利
]

# 无实质业务的客套/确认词（多语言，单词级）——用于"短回复无实质业务"判定。
# 当一封短回复清洗后只由这些词构成时，视为 no_action_reply（不分语言）。
NO_ACTION_COURTESY = [
    # 英
    "thanks", "thank", "thankyou", "ok", "okay", "okey", "received", "noted",
    "understood", "sure", "great", "perfect", "fine", "yes", "no", "cheers", "received.",
    # 德
    "danke", "ja", "nein", "gut", "perfekt", "alles", "klar",
    # 法
    "merci", "oui", "non", "parfait", "bien", "d'accord", "ok.",
    # 西
    "gracias", "sí", "si", "perfecto", "vale", "bien",
    # 意
    "grazie", "sì", "perfetto", "bene",
    # 丹麦 / 北欧
    "tak", "nej", "perfekt", "fint", "ja.",
    # 荷兰
    "bedankt", "dank", "nee", "prima", "oké", "akkoord",
    # 匈牙利
    "köszönöm", "köszi", "igen", "nem", "rendben", "tökéletes",
    # 波兰
    "dzięki", "dziękuję", "tak", "nie", "ok.",
    # 俄
    "спасибо", "да", "нет", "ок", "хорошо",
]

# 退信 / 投递失败（NDR）
BOUNCE_MARKERS = [
    "undeliverable", "mail delivery failed", "delivery has failed",
    "failure notice", "message not delivered", "mailbox is full",
    "mailbox full", "mail delivery subsystem", "returned mail",
    "unzustellbar", "zustellung fehlgeschlagen",   # 德
    "niet afgeleverd",                             # 荷兰
    "échec de la remise", "non distribué",         # 法
    "no se ha entregado",                          # 西
    "mancata consegna",                            # 意
    "leveringen mislykkedes",                      # 丹麦
]
