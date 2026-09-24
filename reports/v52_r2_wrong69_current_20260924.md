# R2 当前版本：历史 128 题错题复测

- 合并基线：59/128 正确；69 道错题。
- 本次仅重测这 69 道：8 道转为 exact match 正确，61 道仍未匹配。
- 错题子集的新 exact match：8/69（11.6%）。
- 模型：Qwen/Qwen3.5-9B；温度：0.0；数据：data/test/filtered_seed4_128/eval_2wikimultihopqa_50.json。
- 固定题单与配置：`results/v52-r2-wrong69-current-20260924-live/baseline_wrong_ids.json`、`run_config.json`；当次代码指纹见同目录 `runtime_code_manifest.json`，基准提交为 `b21b577` 加未提交的当前修改。
- 原先正确的 59 道未重跑；本报告不把子集结果当作新一轮 128 题总准确率。

## 运行状态

| 状态 | 旧错题记录 | 当前复测 |
| --- | ---: | ---: |
| ANSWERED | 42 | 30 |
| ERROR | 1 | 3 |
| INSUFFICIENT | 25 | 30 |
| RUNTIME_ERROR | 1 | 6 |

## 转对的题

dev_379, dev_738, dev_954, dev_3890, dev_4865, dev_5325, dev_9618, dev_11285

## 复核与限制

- 8 道转对题中，`dev_738`、`dev_9618` 的亲属路径，`dev_954`、`dev_5325` 的国籍路径，以及 `dev_379`、`dev_3890`、`dev_11285` 的相应查询均已绑定；其中 `dev_3890` 主要是答案从完整句变成金标准短语。
- `dev_4865` 虽然 exact match 转对，但出生年份查询 Q2 与最终比较查询 Q5 仍为 ACTIVE；这题只能算答案命中，不能算完整证据链修复。
- 旧错题记录中共有 2 道运行/协议错误，本次增至 9 道：3 道 PLAN 依赖校验失败、5 道 `PLAN_REPAIR_EXHAUSTED`、1 道 `ANSWER_BOX_MISSING`。`dev_8009` 的轨迹有 10 次 UPDATE 读取超时，属于模型接口故障影响；其余错误不能直接归因于网络。
- 已核对 69 份轨迹中的模型请求：138 次 UPDATE 和 175 次 MEMORY 均使用当前 `text-27-source-clause` / `text-26-current-query-only` 版本。当前版本同时包含多项代码与提示词调整，本次复测不能把单题变化归因于某一条提示词。温度虽为 0，同一服务端重复调用仍可能有输出差异。

## 逐题对照

| 题号 | 旧答案 | 新答案 | 金标准 | 新状态 | 结果 |
| --- | --- | --- | --- | --- | --- |
| dev_91 | — | foreign minister | United Nations Organization; UN; United Nations; 🇺🇳; UNO | ANSWERED | 仍错 |
| dev_379 | — | Wanderley Oliveira | Wanderley Oliveira | ANSWERED | 转对 |
| dev_448 | — | — | Rimini | INSUFFICIENT | 仍错 |
| dev_738 | — | Princess Therese of Nassau-Weilburg | Therese von Nassau-Weilburg; Princess Therese of Nassau-Weilburg | ANSWERED | 转对 |
| dev_954 | no | Yes | yes | ANSWERED | 转对 |
| dev_1060 | Humphrey Edwardes-Jones | Humphrey Edwardes-Jones | Ángel De La Torre; Ángel de la Torre; Angel de la Torre | ANSWERED | 仍错 |
| dev_1328 | — | — | Dublin; Dublin city; City of Dublin | INSUFFICIENT | 仍错 |
| dev_1456 | Baptist Noel, 4th Earl of Gainsborough | — | Baptist Noel, 3rd Earl of Gainsborough | INSUFFICIENT | 仍错 |
| dev_1850 | — | — | Kate David; Ekaterina Nagy von Cziser; Kitty Fattini; Käthe von Nagy; Kate de Nagy; Kathe de Nagy; Kató Nagy | INSUFFICIENT | 仍错 |
| dev_2065 | — | — | 1800 | INSUFFICIENT | 仍错 |
| dev_2261 | Knife In The Water | — | Born to Speed; Born To Speed | ERROR | 仍错 |
| dev_2828 | Russian | Russian | USSR; Soviet Union; Soviets; Soviet; The Soviets; the Union of Soviet Socialist Republics; U.S.S.R.; U.S.S.R; SU; CCCP; the Soviet Union; СССР; Union of Soviet Socialist Republics | ANSWERED | 仍错 |
| dev_2945 | French | Charles II of Naples | Sicily, Kingdom of; Regno di Napoli; Kingdom of Sicily; Kingdom of Naples | ANSWERED | 仍错 |
| dev_2955 | Czechoslovakia | — | Hradec Kralove; Hradec Králové | RUNTIME_ERROR | 仍错 |
| dev_3133 | — | Maria Anna of Bavaria | Graz | ANSWERED | 仍错 |
| dev_3343 | — | — | Mühlburg | INSUFFICIENT | 仍错 |
| dev_3606 | Thailand | Bang Khonthi District, Samut Songkhram Province, Thailand | Bang Khonthi; Bang Khonthi District | ANSWERED | 仍错 |
| dev_3668 | Empress Maria Feodorovna of Russia | Empress Maria Feodorovna of Russia | Maria Feodorovna (Dagmar of Denmark); Dagmar of Denmark; Princess Dagmar of Denmark; Maria Feodorovna; Princess Dagmar of Schleswig-Holstein-Sonderburg-Glücksburg | ANSWERED | 仍错 |
| dev_3890 | He was executed by firing squad. | execution by firing squad | execution by firing squad; Execution by firing squad; firing squad | ANSWERED | 转对 |
| dev_3921 | — | — | Fårö | INSUFFICIENT | 仍错 |
| dev_4020 | — | — | Greater Adelaide; Adelaide | INSUFFICIENT | 仍错 |
| dev_4021 | — | — | Shahriyar; Shahriyar (son of Khosrow II) | INSUFFICIENT | 仍错 |
| dev_4060 | Toronto, Ontario | — | the five boroughs; City of New York; New York; NY City; New York City; NYC; Big Apple; New York, New York | INSUFFICIENT | 仍错 |
| dev_4237 | — | — | Fulbright Program; Fulbright Scholarship; Fulbright Fellowship | INSUFFICIENT | 仍错 |
| dev_4288 | Queen Sirikit | Princess Ubolratana Rajakanya | Sirikit Kitiyakara; Sirikit | ANSWERED | 仍错 |
| dev_4311 | Fougères, Ille-et-Vilaine | — | Fougères | INSUFFICIENT | 仍错 |
| dev_4478 | England | — | Essex | RUNTIME_ERROR | 仍错 |
| dev_4680 | Nina Škottová | Nina Škottová | Henry Strutt, 2Nd Baron Belper; Henry Strutt, 2nd Baron Belper | ANSWERED | 仍错 |
| dev_4784 | Russia | — | Moscow, Russian Federation; City of Moscow; Moskva, Russia; Moscow; Moskva Federal City, Russia; Moscow, USSR; Moskva; Moscow, Russia; Moscow, Soviet Union; Москва; Moscow, Russian SFSR | INSUFFICIENT | 仍错 |
| dev_4865 | Fog And Sun | No. 1 Of The Secret Service | No. 1 Of The Secret Service; No. 1 of the Secret Service | ANSWERED | 转对 |
| dev_4961 | — | — | London, England; London; London, United Kingdom; London, UK | INSUFFICIENT | 仍错 |
| dev_5002 | no | — | yes | RUNTIME_ERROR | 仍错 |
| dev_5027 | — | — | Warkworth, Northumberland; Warkworth | INSUFFICIENT | 仍错 |
| dev_5132 | — | — | George Louis, Prince of Nassau-Dillenburg; Georg Ludwig Graf von Nassau-Dillenburg | INSUFFICIENT | 仍错 |
| dev_5325 | Indian | Singapore | Singapore; SG; Lion City; SGP; sg; Garden City; 🇸🇬; Republic of Singapore; Singapore City | ANSWERED | 转对 |
| dev_5343 | — | — | Elizabeth Woodville; Elizabeth Wydville; Elizabeth Widvile; Elizabeth Wydeville | INSUFFICIENT | 仍错 |
| dev_5351 | Leopold I | Leopold I | Emperor of Germany Ferdinand III; imperatore del Sacro romano impero Ferdinand III; Kaiser Ferdinand III. Heiliges Römisches Reich; King Ferdinand III of Hungary; keizer van het Duitse Rijk Ferdinand III; Holy Roman Emperor Ferdinand III; Ferdinand III, Holy Roman Emperor; King of Bohemia Ferdinand III; Ferdinand III; empereur germanique Ferdinand III; King of Hungary Ferdinand III | ANSWERED | 仍错 |
| dev_5416 | NO | — | yes | INSUFFICIENT | 仍错 |
| dev_5534 | Annedal, Gothenburg, Sweden | Annedal, Gothenburg, Sweden | Göteborg; Gothenburg | ANSWERED | 仍错 |
| dev_5885 | — | — | To Play or to Die; To Play Or To Die; To Play or To Die | INSUFFICIENT | 仍错 |
| dev_6782 | — | Alekos Sakellarios | First Cemetery of Athens | ANSWERED | 仍错 |
| dev_6785 | no | No | yes | ANSWERED | 仍错 |
| dev_6901 | Hauteville | — | Coutances | INSUFFICIENT | 仍错 |
| dev_7046 | — | — | Chantal de Chevron-Villette | INSUFFICIENT | 仍错 |
| dev_7146 | American-Canadian | American-Canadian | U.S.A.; American; US of A; US; United States of America; USA; the United States of America; United States; the US; 🇺🇸; the United States; U.S.; America; the USA | ANSWERED | 仍错 |
| dev_7217 | Oliba I of Carcassonne | — | Bello of Carcassonne | INSUFFICIENT | 仍错 |
| dev_7221 | Gli Eroi Del Doppio Gioco | Gli Eroi Del Doppio Gioco | Studujeme za školou; Studujeme Za Školou | ANSWERED | 仍错 |
| dev_8009 | And The Heavens Above Us | — | Paasa Paravaigal | RUNTIME_ERROR | 仍错 |
| dev_8199 | Lwów (Lviv), [Poland] (now Ukraine) | — | Lvov; Lemberik; Lwow; L'vov; Lwów; Leopol; L'viv; Lemberg; Lviv | INSUFFICIENT | 仍错 |
| dev_8530 | Andrew II of Hungary | — | Béla III of Hungary; Béla III (114823 April 1196) was King of Hungary; Bela III of Hungary | INSUFFICIENT | 仍错 |
| dev_9193 | — | — | The Secret Bride; Secret Bride | ERROR | 仍错 |
| dev_9348 | — | — | The Whisperers; Whisperers | ERROR | 仍错 |
| dev_9618 | Leopoldo Torre Nilsson | Leopoldo Torres Ríos | Leopoldo Torres Ríos; Leopoldo Torres Rios | ANSWERED | 转对 |
| dev_9742 | Atheetham | — | Otec neznámý aneb cesta do hlubin duše výstrojního náčelníka; Otec Neznámý Aneb Cesta Do Hlubin Duše Výstrojního Náčelníka | RUNTIME_ERROR | 仍错 |
| dev_10047 | — | — | yes | RUNTIME_ERROR | 仍错 |
| dev_10191 | de Vienne | — | Tinseltown; Hollywood, California; Hollywood | INSUFFICIENT | 仍错 |
| dev_10404 | Modern Mothers | — | Slaughter Rule; The Slaughter Rule | INSUFFICIENT | 仍错 |
| dev_10526 | — | Battle of Göllheim | Göllheim | ANSWERED | 仍错 |
| dev_10872 | Young Australian of the Year Award | Young Australian of the Year Award | Young Australian of the Year | ANSWERED | 仍错 |
| dev_10994 | — | The Man Unconquerable | Woman Without A Past; Woman Without a Past | ANSWERED | 仍错 |
| dev_11141 | Chaplinesque | — | Chaplinesque, My Life And Hard Times; Chaplinesque, My Life and Hard Times | INSUFFICIENT | 仍错 |
| dev_11153 | 16 January 1857 | 16 January 1857 | 28 August 1827 | ANSWERED | 仍错 |
| dev_11285 | — | The Night Of The Party | Night of the Party; The Night Of The Party; The Night of the Party | ANSWERED | 转对 |
| dev_11393 | Los Angeles | Canada | the five boroughs; City of New York; New York; NY City; New York City; NYC; Big Apple; New York, New York | ANSWERED | 仍错 |
| dev_11630 | — | — | Bostonia; The Hub of the Universe; Boston, MA; The Athens of America; The Walking City; The Hub; Boston, Massachusetts; The Cradle of Liberty; Boston; Boston, Mass.; The Cradle of Modern America; Beantown | INSUFFICIENT | 仍错 |
| dev_11641 | China | — | Loyang; Luoyang | INSUFFICIENT | 仍错 |
| dev_11888 | Princess Adelheid of Schaumburg-Lippe | — | Princess Louise Caroline of Hesse-Kassel; Luise Karoline von Hessen-Kassel | INSUFFICIENT | 仍错 |
| dev_12150 | Hot Rod Rumble | Hot Rod Rumble | Nagalit ang Buwan sa Haba ng Gabi; Nagalit Ang Buwan Sa Haba Ng Gabi | ANSWERED | 仍错 |
| dev_12309 | Lőcse | Lőcse (today Levoča, Slovakia) | Levoča | ANSWERED | 仍错 |
