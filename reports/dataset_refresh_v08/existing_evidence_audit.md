# 旧题关键证据人工复核

判断标准：给人类题目和完整相关段落，但不给 Wikidata 三元组或标准答案，不依赖外部常识，能否推出唯一且与标准答案一致的结果。官方 supporting_facts 的标签和 evidences 三元组仅用于检查标注，不作为模型可见的证明。

检查全部 30 道 V08 错题：12 道存在证据缺失、歧义或 gold 别名口径问题；其余 18 道证据足以作答。另补查 5 道已答对但存在文本/人物链接问题的候选。

| ID | 类型 | V08 EM | 人类可答性 | 原因 |
|---|---|---:|---|---|
| dev_91 | compositional | 0 | AMBIGUOUS_ATTRIBUTE | 丈夫原文有联合国、International IDEA、红十字会、市议会等多个工作单位，问题没有时间限定，UN 并非唯一可推出的答案。 |
| dev_1240 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_1629 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_2065 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_2828 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_2945 | compositional | 0 | MISSING_RELATION | 获授 Kingdom of Sicily 内的封地、担任摄政等头衔，不能直接建立题目所问 nationality/citizenship。 |
| dev_3792 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_3921 | compositional | 0 | MISSING_RELATION | Fårö 是拍摄地点，未建立导演死于该岛的关系。 |
| dev_4020 | compositional | 0 | MISSING_RELATION | 原文说 former partner 与 grew up in Adelaide，既没有明确 husband，也没有明确出生于 Adelaide。 |
| dev_4060 | compositional | 0 | GOLD_ALIAS_MISMATCH | 传记明确出生于 Glen Cove, Long Island, New York；官方答案是 New York，但 FlashRAG 的答案别名把它限制成 New York City，不能要求读者把 Glen Cove 替换为 NYC。 |
| dev_4167 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_4298 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_4311 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_4478 | compositional | 0 | MISSING_RELATION | Essex 是学校所在地，未建立导演出生于 Essex 的关系。 |
| dev_4784 | compositional | 0 | MISSING_RELATION | Moscow 是制片厂地点；同时有两位导演，未给出 Moscow 出生关系。 |
| dev_4961 | compositional | 1 | MISSING_RELATION | 原文提到 London Films 公司与英国电影工作经历，没有死于 London 的关系。 |
| dev_5002 | bridge_comparison | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_5027 | compositional | 0 | MISSING_RELATION | Warkworth 是被授予的城堡/领地，未说父亲死于那里。 |
| dev_5416 | comparison | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_5853 | bridge_comparison | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_6782 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_6785 | bridge_comparison | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_6901 | compositional | 1 | MISSING_RELATION | near Coutances 是人物身份/居住背景，没有出生于 Coutances 的关系。 |
| dev_7046 | inference | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_7403 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_8199 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_9618 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_9742 | bridge_comparison | 1 | ENTITY_MISMATCH | Atheetham 的电影导演 Devan Nair 被连接到同名新加坡总统的传记；共享名字不足以确立是同一人。 |
| dev_10047 | bridge_comparison | 0 | MISSING_RELATION | Hitchcock 的美国国籍未在原文建立。官方引用的美国国家电影登记处也不能证明本人具有美国国籍。 |
| dev_10191 | compositional | 1 | MISSING_RELATION | 原文提到 1937 年迁居 Hollywood 和工作经历，没有死于 Hollywood 的关系。 |
| dev_10404 | bridge_comparison | 1 | ENTITY_MISMATCH | The Slaughter Rule 的导演 Alex Smith 被连接到 Alex Smith (golfer)；高尔夫球员的日期不能用于电影导演比较。 |
| dev_10526 | compositional | 0 | ANSWERABLE | 原文能建立所问关系并支持标准答案；本次失分来自提取、绑定、可见性、比较或格式。 |
| dev_11393 | compositional | 0 | AMBIGUOUS_ENTITY | 歌曲同时列主唱 the Weeknd 与客串 Lana Del Rey，题目只说 performer，无法唯一指定标准答案对应的 Lana。 |
| dev_11630 | compositional | 0 | MISSING_RELATION | Boston 是戏剧公司工作地点，未说明导演出生于 Boston。 |
| dev_11641 | compositional | 0 | MISSING_RELATION | Luoyang 在收复城市的战争叙述中出现，未说明 Daizong 出生于此。 |

## 存疑题的原始标注

### dev_91

Where does Karin Stoltenberg's husband work at?

官方答案：United Nations；本次接受别名：United Nations Organization / UN / United Nations / 🇺🇳 / UNO。

**丈夫原文有联合国、International IDEA、红十字会、市议会等多个工作单位，问题没有时间限定，UN 并非唯一可推出的答案。**

- Karin Stoltenberg，官方句号 0：Karin Stoltenberg, née Heiberg (23 November 1931 – 17 October 2012), was a Norwegian geneticist, politician and public official noted for her efforts to develop a coherent family policy in Norway, feminist activities, and for being the mother of prime minister Jens Stoltenberg, and wife of foreign minister Thorvald Stoltenberg.
- Thorvald Stoltenberg，官方句号 4：In 1990 he became the United Nations High Commissioner for Refugees, but served only one year before rejoining the Norwegian government.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_2945

What nationality is Peter Tempesta's father?

官方答案：Kingdom of Sicily；本次接受别名：Sicily, Kingdom of / Regno di Napoli / Kingdom of Sicily / Kingdom of Naples。

**获授 Kingdom of Sicily 内的封地、担任摄政等头衔，不能直接建立题目所问 nationality/citizenship。**

- Peter Tempesta，官方句号 1：He was the eighth son of Charles II of Naples and Mary of Hungary (see Elizabeth of Sicily).
- Charles II of Naples，官方句号 3：His father granted Charles the Principality of Salerno in the Kingdom of Sicily (or "Regno") in 1272 and made him regent in Provence and Forcalquier in 1279.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_3921

Where did the director of film This Can'T Happen Here die?

官方答案：Fårö；本次接受别名：Fårö。

**Fårö 是拍摄地点，未建立导演死于该岛的关系。**

- This Can't Happen Here，官方句号 1：Here( also released as High Tension in English) is a 1950 Swedish film directed by Ingmar Bergman.
- Ingmar Bergman，官方句号 10：onward were filmed on the island of Fårö.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_4020

What is the place of birth of Kaarin Fairfax's husband?

官方答案：Adelaide；本次接受别名：Greater Adelaide / Adelaide。

**原文说 former partner 与 grew up in Adelaide，既没有明确 husband，也没有明确出生于 Adelaide。**

- Kaarin Fairfax，官方句号 4：Fairfax is the former partner of Australian musician Paul Kelly—they met in 1988— their two children are Madeleine (born 1991) and Memphis (born 1993).
- Paul Kelly (Australian musician)，官方句号 10：After growing up in Adelaide, Kelly travelled around Australia before settling in Melbourne in 1976.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_4060

Where was the director of film Munchie born?

官方答案：New York；本次接受别名：the five boroughs / City of New York / New York / NY City / New York City / NYC / Big Apple / New York, New York。

**传记明确出生于 Glen Cove, Long Island, New York；官方答案是 New York，但 FlashRAG 的答案别名把它限制成 New York City，不能要求读者把 Glen Cove 替换为 NYC。**

- Munchie，官方句号 0：Munchie is a 1992 comedy film directed by Jim Wynorski and it is a name-only sequel to the 1987 film, Munchies.
- Jim Wynorski，官方句号 0：Jim Wynorski (born August 14, 1950 in Glen Cove, Long Island, New York) is an American screenwriter, director, and producer.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_4478

What is the place of birth of the director of film Julius Caesar (1970 Film)?

官方答案：Essex；本次接受别名：Essex。

**Essex 是学校所在地，未建立导演出生于 Essex 的关系。**

- Julius Caesar (1970 film)，官方句号 0：Julius Caesar is a 1970 British independent film adaptation of William Shakespeare's play of the same name, directed by Stuart Burge from a screenplay by Robert Furnival.
- Stuart Burge，官方句号 1：The son of H. O. Burge, by his marriage to K. M. Haig, Burge was educated at Eagle House School, Sandhurst, and Felsted School, Essex, then trained for an acting career at the Old Vic, 1936–37, and at Oxford Rep, 1937–38.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_4784

Where was the director of film Taras Bulba (1924 Film) born?

官方答案：Moscow；本次接受别名：Moscow, Russian Federation / City of Moscow / Moskva, Russia / Moscow / Moskva Federal City, Russia / Moscow, USSR / Moskva / Moscow, Russia / Moscow, Soviet Union / Москва / Moscow, Russian SFSR。

**Moscow 是制片厂地点；同时有两位导演，未给出 Moscow 出生关系。**

- Taras Bulba (1924 film)，官方句号 0：Taras Bulba is a 1924 German silent adventure film directed by Vladimir Strizhevsky and Joseph N. Ermolieff and starring J.N. Douvan-Tarzow, Oscar Marion and Clementine Plessner.
- Joseph N. Ermolieff，官方句号 1：Ermolieff was a prominent figure in early Russian cinema during the Imperial era, owning large studios in Yalta and Moscow.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_4961

Where did the director of film Yamata die?

官方答案：London；本次接受别名：London, England / London / London, United Kingdom / London, UK。

**原文提到 London Films 公司与英国电影工作经历，没有死于 London 的关系。**

- Yamata，官方句号 0：Yamata is a 1919 Hungarian silent drama film directed by Alexander Korda and starring Emil Fenyvessy, Ila Lóth and Gábor Rajnay.
- Alexander Korda，官方句号 4：He was the founder of London Films and, post-war, the owner of British Lion Films, a film distribution company.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_5027

Where was the place of death of Henry Percy, 3Rd Baron Percy's father?

官方答案：Warkworth；本次接受别名：Warkworth, Northumberland / Warkworth。

**Warkworth 是被授予的城堡/领地，未说父亲死于那里。**

- Henry Percy, 3rd Baron Percy，官方句号 0：Henry Percy, 3rd Baron Percy of Alnwick( c. 1321– 1368), was the eldest son of Henry de Percy, 2nd Baron Percy( 1301 – 1352), and his wife, Idoine de Clifford( Idonea in Latin and also in English), daughter of Robert de Clifford, 1st Baron de Clifford.
- Henry Percy, 2nd Baron Percy，官方句号 8：Was granted, by Edward III, the castle and barony of Warkworth in 1328.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_6901

What is the place of birth of William Iron Arm's father?

官方答案：Coutances；本次接受别名：Coutances。

**near Coutances 是人物身份/居住背景，没有出生于 Coutances 的关系。**

- William Iron Arm，官方句号 1：One of twelve sons of Tancred of Hauteville, he journeyed to the Mezzogiorno with his younger brother Drogo in the first half of the eleventh century (c.1035), in response to requests for help made by fellow Normans under Rainulf Drengot, count of Aversa.
- Tancred of Hauteville，官方句号 2：He was a minor noble near Coutances in the Cotentin.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_9742

Which film has the director who was born later, Atheetham or Otec Neznámý Aneb Cesta Do Hlubin Duše Výstrojního Náčelníka?

官方答案：Otec Neznámý Aneb Cesta Do Hlubin Duše Výstrojního Náčelníka；本次接受别名：Otec neznámý aneb cesta do hlubin duše výstrojního náčelníka / Otec Neznámý Aneb Cesta Do Hlubin Duše Výstrojního Náčelníka。

**Atheetham 的电影导演 Devan Nair 被连接到同名新加坡总统的传记；共享名字不足以确立是同一人。**

- Atheetham，官方句号 0：Atheetham is a 2007 Indian Malayalam film directed by Devan Nair.
- Otec neznámý aneb cesta do hlubin duše výstrojního náčelníka，官方句号 0：Otec neznámý aneb cesta do hlubin duše výstrojního náčelníka is a Czech comedy film directed by Karel Kachyňa.
- Devan Nair，官方句号 1：( 5 August 1923 – 6 December 2005), also known as C. V. Devan Nair, was a Malaysian- Singaporean politician.
- Karel Kachyňa，官方句号 0：Karel Kachyňa (1 May 1924 – 12 March 2004) was a Czech film director.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_10047

Do director of film Ten9Eight: Shoot For The Moon and director of film Sabotage (1936 Film) share the same nationality?

官方答案：yes；本次接受别名：yes。

**Hitchcock 的美国国籍未在原文建立。官方引用的美国国家电影登记处也不能证明本人具有美国国籍。**

- Ten9Eight: Shoot for the Moon，官方句号 0：Ten9 Eight: Shoot for the Moon is a 2009 documentary film about inner New York City teenagers who compete in an annual business plan competition run by the Network for Teaching Entrepreneurship( NFTE) written, directed, and produced by Mary Mazzio.
- Sabotage (1936 film)，官方句号 0：Sabotage, also released as The Woman Alone, is a 1936 British espionage thriller film directed by Alfred Hitchcock and starring Sylvia Sidney, Oskar Homolka, and John Loder.
- Mary Mazzio，官方句号 0：Mary Mazzio is an American documentary filmmaker, attorney, and a rower for the United States in the 1992 Olympics.
- Alfred Hitchcock，官方句号 5：His first successful film,( 1927), helped to shape the thriller genre, while his 1929 film," Blackmail", was the first British.
- Alfred Hitchcock，官方句号 16：By 2018 eight of his films had been selected for preservation in the United States National Film Registry, including his personal favourite," Shadow of a Doubt"( 1943).

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_10191

Where did the director of film Alexander The Great (1956 Film) die?

官方答案：Hollywood；本次接受别名：Tinseltown / Hollywood, California / Hollywood。

**原文提到 1937 年迁居 Hollywood 和工作经历，没有死于 Hollywood 的关系。**

- Alexander the Great (1956 film)，官方句号 1：the Great written, produced and directed by Robert Rossen.
- Robert Rossen，官方句号 4：After directing and writing for the stage in New York, Rossen moved to Hollywood in 1937.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_10404

Which film has the director died earlier, Modern Mothers or The Slaughter Rule?

官方答案：The Slaughter Rule；本次接受别名：Slaughter Rule / The Slaughter Rule。

**The Slaughter Rule 的导演 Alex Smith 被连接到 Alex Smith (golfer)；高尔夫球员的日期不能用于电影导演比较。**

- Modern Mothers，官方句号 0：Modern Mothers is a 1928 American silent drama film, directed by Phil Rosen.
- The Slaughter Rule，官方句号 0：The Slaughter Rule is a 2002 independent film directed by Alex Smith and Andrew J. Smith and starring Ryan Gosling and David Morse.
- Phil Rosen，官方句号 0：Philip E. Rosen( May 8, 1888 – October 22, 1951) was an American film director and cinematographer.
- Alex Smith (golfer)，官方句号 0：Alexander Smith (28 January 1874 – 21 April 1930) was a Scottish-American professional golfer who played in the late 19th and early 20th century.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_11393

Where was the performer of song Stargirl Interlude born?

官方答案：New York；本次接受别名：the five boroughs / City of New York / New York / NY City / New York City / NYC / Big Apple / New York, New York。

**歌曲同时列主唱 the Weeknd 与客串 Lana Del Rey，题目只说 performer，无法唯一指定标准答案对应的 Lana。**

- Stargirl Interlude，官方句号 0："Stargirl Interlude" is a song by Canadian singer the Weeknd featuring American singer Lana Del Rey.
- Lana Del Rey，官方句号 2：Born in New York City and raised in Upstate New York, Del Rey returned to New York City in 2005 to begin her music career.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_11630

Where was the director of film Trick For Trick born?

官方答案：Boston；本次接受别名：Bostonia / The Hub of the Universe / Boston, MA / The Athens of America / The Walking City / The Hub / Boston, Massachusetts / The Cradle of Liberty / Boston / Boston, Mass. / The Cradle of Modern America / Beantown。

**Boston 是戏剧公司工作地点，未说明导演出生于 Boston。**

- Trick for Trick，官方句号 0：Trick for Trick is a 1933 American mystery film directed by Hamilton MacFadden, written by Howard J. Green, and starring Ralph Morgan, Victor Jory, Sally Blane, Tom Dugan, Luis Alberni and Edward Van Sloan.
- Hamilton MacFadden，官方句号 5：Soon after graduating, he became producer of the American Theatre Company, which presented plays for 10 weeks in the Boston area.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。

### dev_11641

What is the place of birth of Consort Dugu's husband?

官方答案：Luoyang；本次接受别名：Loyang / Luoyang。

**Luoyang 在收复城市的战争叙述中出现，未说明 Daizong 出生于此。**

- Consort Dugu，官方句号 1：(Li Chu).
- Emperor Daizong of Tang，官方句号 2：Emperor Daizong was the eldest son of Emperor Suzong – the first Emperor of the Tang dynasty to succeed as the eldest child, and during the Anshi Rebellion (which Emperor Suzong's entire reign was dedicated to fighting), he served as a general of Tang and Huige joint operations that recaptured the capital Chang'an and the eastern capital Luoyang from the rebel state of Yan, and the Anshi Rebellion was finally put down early in his own reign, in 763.

判断已检查完整支撑段落；完整文本保存在 [结构化复核](existing_evidence_audit.json) 中。


这里的 ANSWERABLE 不等于模型应当零失误，存疑也不等于标准答案在现实世界必然错误；判断限定为本题提供的证据。
