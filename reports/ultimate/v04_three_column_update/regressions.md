# 三列 UPDATE 相对 V04 的全部退步题

以下是实际轨迹中的观察，未把单轮模型输出变化强行归因于某一代码分支。

## dev_1328

Where did Ragnall Guthfrithson's father die?

Gold：["Dublin", "Dublin city", "City of Dublin"]
V04：Dublin；本轮：unknown。

V04 的首窗口抽取了父亲死于 Dublin；本轮首窗口只抽取父亲身份，未抽取死亡地点，次窗口只有死亡年份；Q2 未绑定，最终输出 unknown。观察为抽取/证据覆盖差异，未见状态误路由。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_1328-v5_predicted-original-7f7eae42.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_1328-v5_predicted-original-cd624e4f.json`

## dev_8199

Where was the director of film The Devil (1972 Film) born?

Gold：["Lvov", "Lemberik", "Lwow", "L'vov", "Lwów", "Leopol", "L'viv", "Lemberg", "Lviv"]
V04：Lwów；本轮：Lwów (Lviv)。

两版都抽取 Lwów (Lviv) 相关地点，Q2 的绑定均出现 SOURCE_CHECK_NOT_ELIGIBLE；本轮最终输出 Lwów (Lviv)，项目 EM 未将组合写法与单一别名匹配。不能将此题简单归为没有读到地点。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_8199-v5_predicted-original-a59570e2.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_8199-v5_predicted-original-40ccdcd9.json`

## dev_4865

Which film has the director born later, No. 1 Of The Secret Service or Fog And Sun?

Gold：["No. 1 Of The Secret Service", "No. 1 of the Secret Service"]
V04：No. 1 Of The Secret Service；本轮：None。

本轮只有两位导演身份进入链，缺少其生日；ANSWER 两次达到 4096 token 上限（finish_reason=length），均没有 boxed answer，最终 ERROR。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_4865-v5_predicted-original-4643cb5f.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_4865-v5_predicted-original-2c7d061d.json`

## dev_5534

Where was the director of film Please (Film) born?

Gold：["Göteborg", "Gothenburg"]
V04：Gothenburg；本轮：。

已得到 ?birthplace=Annedal, Gothenburg, Sweden；ANSWER 长篇讨论地点粒度后达到 4096 token 上限，现有解析器得到空答案，未计为正确。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_5534-v5_predicted-original-d92121b1.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_5534-v5_predicted-original-3d8928be.json`

## dev_4021

Who is the paternal grandfather of Shahrbanu?

Gold：["Shahriyar", "Shahriyar (son of Khosrow II)"]
V04：Shahriyar；本轮：Yazdegerd III。

两版均出现把 Husayn ibn Ali 绑定为父亲的错误中间状态；V04 后续得到 Shahriyar。本轮 RECALL 返回 NONE，后续又抽取无关祖父事实，Q2 未完成，最终回答 Yazdegerd III。观察到身份/关系与候选选择差异，不据单轮认定唯一根因。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_4021-v5_predicted-original-3e64d7a2.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_4021-v5_predicted-original-1b1f3a1e.json`

## dev_10404

Which film has the director died earlier, Modern Mothers or The Slaughter Rule?

Gold：["Slaughter Rule", "The Slaughter Rule"]
V04：The Slaughter Rule；本轮：Modern Mothers。

V04 将 Alexander Smith 的死亡年份用于 The Slaughter Rule 导演链并取得基准正确答案；本轮没有抽取 Alex/Andrew Smith 的死亡日期，最终猜 Modern Mothers。属于目标身份与证据覆盖差异，V04 的答对也不代表其链条语义必然正确。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_10404-v5_predicted-original-783c11b6.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_10404-v5_predicted-original-d6b58009.json`

## dev_7217

Who is the paternal grandfather of Acfred I Of Carcassonne?

Gold：["Bello of Carcassonne"]
V04：Bello of Carcassonne；本轮：Sunifred I of Barcelona。

本轮首窗口把 Acfred II 相关来源用于 Acfred I，后续把 Bello 与 Oliba I 的关系抽成兄弟；HIGH 最终错误地把父亲 Oliba I 绑定为祖父，ANSWER 又猜 Sunifred I。V04 曾保存 Bello 是 Oliba I 父亲的直接事实。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_7217-v5_predicted-original-62e72398.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_7217-v5_predicted-original-fb132a7c.json`

## dev_7331

Who is the paternal grandfather of Władysław I Of Płock?

Gold：["Siemowit III, Duke of Masovia"]
V04：Siemowit III, Duke of Masovia；本轮：Ziemowit I, Prince of Masovia。

本轮最初正确绑定 Siemowit III，次窗口从 D37 产生了“Siemowit IV 是 Ziemowit I 之子”的更正事实；HIGH 错误核验并 REBIND 为 Ziemowit I，最终被覆盖。V04 对应后续输出包含错误来源 ID，被拒绝而保留先前答案。观察为新抽取内容和来源判断问题。

V04 轨迹：`results/ultimate/v04_joint_source_memory_full128_retry3/trajectories/ultimate-v04-joint-source-memory-full128-retry3-dev_7331-v5_predicted-original-2a83df93.json`
本轮轨迹：`results/ultimate/v04_three_column_update_full128_r1/trajectories/v04-three-column-update-full128-r1-dev_7331-v5_predicted-original-fbc29aa9.json`

