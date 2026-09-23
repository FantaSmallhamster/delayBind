"""Build a read-only, self-contained diagnosis of the merged 128-question run.

The 21 retry records replace their corresponding original records. This script
does not run models or modify the experiment's trajectories/results.
"""

from __future__ import annotations

import csv
import html
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "results/v52-r2-full128-member-graph-text22-20260923/results.jsonl"
RETRY = ROOT / "results/v52-r2-full128-member-graph-text22-retry5-c2-20260923/results.jsonl"
QUESTIONS = ROOT / "data/test/filtered_seed4_128/questions.csv"
OUTPUT = ROOT / "reports/v52_r2_128_merged_wrong_cases_20260923.html"

# Stages name the first independently observable failure, not every downstream
# symptom. Claims about absent evidence refer to stored facts, not the corpus.
DIAGNOSES = {
    "dev_91": ("UPDATE/MEMORY", "只抽到 Thorvald Stoltenberg 任外交部长，没有抽到联合国任职；Q2 又把人名绑定为工作地点，ANSWER 因缺地点返回 UNKNOWN。"),
    "dev_379": ("证据冲突/ANSWER", "两人的生日都绑定为 1978-12-04，但事实中还有先后顺序断言；日期与先后断言冲突，最终无法作出可靠比较而返回 UNKNOWN。"),
    "dev_448": ("UPDATE/MEMORY", "抽到‘Ginevra 是 Sigismondo 的妻子’这一逆表达，却没有完成丈夫 Q1 的绑定；只有死亡日期、没有 Rimini 地点，Q2 无从回答。"),
    "dev_738": ("UPDATE/MEMORY", "把 Alexandra 本人绑定成其父亲，属于角色/方向错误；后续祖母路径因此断裂，且没有祖母事实。"),
    "dev_954": ("PLAN/MEMORY/ANSWER", "PLAN 多加了应交给 ANSWER 的最终比较节点；Ding 同时绑定 Chinese、German，Lucae 的国籍又错绑为本人。ANSWER 最后据污染值答 no。"),
    "dev_1060": ("UPDATE/ANSWER", "只绑定 Humphrey 的生日；另一人 Ángel 的生日缺失，反而存入不相干的 Facundo de la Torre。ANSWER 在缺少对照日期时仍猜 Humphrey。"),
    "dev_1328": ("UPDATE/MEMORY", "Q1 将两个不同 Gofraid 都当作父亲，未消歧；Q2 仅有死亡年份、缺 Dublin 地点，故 UNKNOWN。"),
    "dev_1456": ("UPDATE/事实冲突", "工作记忆将 Anthony 的父亲记为 Baptist Noel 第四代，金标准为第三代；另有 Henry 是第四代之子的事实，代际关系相互冲突，错误值被绑定。"),
    "dev_1850": ("UPDATE", "导演已绑定，但配偶事实退化为‘Constantin 的配偶是 Constantin 的配偶’的同义反复，未抽出 Käthe von Nagy。"),
    "dev_2065": ("UPDATE", "成功找到父亲 James Power；没有记录其出生年份 1800，第二跳缺证据。"),
    "dev_2261": ("UPDATE/MEMORY/ANSWER", "把 Roman Polanski 错连到 Born To Speed，两个电影导演关系被污染；Edward L. Cahn 的出生日期缺失，ANSWER 仍在证据不全时选 Knife In The Water。"),
    "dev_2828": ("UPDATE/MEMORY", "Prince Or Clown 绑定了两名导演，国籍节点又被错误绑定为人名；最终使用 Russian，与金标准 Soviet/USSR 不一致。"),
    "dev_2945": ("UPDATE/MEMORY", "父亲 Charles II of Naples 已找到，但国籍节点被绑定成父亲名字；ANSWER 从 French 事实作答，与标注的王国归属答案不一致。"),
    "dev_2955": ("UPDATE/答案粒度", "只记录并绑定 Otakar Vávra 出生于 Czechoslovakia，未保留金标准所需的城市 Hradec Králové；ANSWER 沿用过粗地点。"),
    "dev_3133": ("UPDATE", "母亲 Maria Anna 已绑定，但没有抽到其墓葬地 Graz，第二跳无事实。"),
    "dev_3343": ("UPDATE/证据冲突", "父亲绑定 Hesso，后续又出现 Jacob 是其父亲的相反事实；只有 Hesso 死亡年份，缺 Mühlburg 地点。"),
    "dev_3606": ("UPDATE/答案粒度", "只抽到导演出生于 Thailand，未抽到区县 Bang Khonthi；ANSWER 返回国家而非金标准地点。"),
    "dev_3668": ("答案规范化", "绑定值 Empress Maria Feodorovna of Russia 与金标准 Maria Feodorovna/Dagmar of Denmark 指向同一人，但 exact match 别名表未收录这一头衔表达。"),
    "dev_3890": ("答案规范化", "事实和绑定支持 firing squad；ANSWER 输出完整句‘He was executed by firing squad.’，金标准只收短语，exact match 判错。"),
    "dev_3921": ("UPDATE/MEMORY", "把 Ian Barry 与 Ingmar Bergman 都连为该片导演，且 Q2 出现 Stockholm；记录中没有 Fårö，冲突/错误分支导致 UNKNOWN。"),
    "dev_4020": ("UPDATE/MEMORY", "Kaarin Fairfax 的 former partner 被当作丈夫候选但 MEMORY 未绑定；另有 Paul Kelly 生于 Sydney，与金标准 Adelaide 也不符。"),
    "dev_4021": ("UPDATE/MEMORY", "Q1 抽到 Yazdegerd III 是父亲却未绑定；Q2 未记录其父亲 Shahriyar，祖父路径中断。"),
    "dev_4060": ("UPDATE/事实错误", "记录的出生地是 Toronto, Ontario，而金标准是 New York；错误第二跳事实进入绑定并被直接回答。"),
    "dev_4237": ("UPDATE", "导演 Michael Rubbo 已绑定，但没有获奖 Fulbright 的事实，反而记录‘未提及获奖’，故 UNKNOWN；旧 429 错误字段属于被替换记录，不是本次终态。"),
    "dev_4288": ("答案规范化", "Queen Sirikit 与金标准 Sirikit/Sirikit Kitiyakara 指向同一人；头衔未在 exact-match 别名中，属于评测表达差异。"),
    "dev_4311": ("答案规范化/事实冲突", "Fougères, Ille-et-Vilaine 含金标准 Fougères，精确匹配仍判错；另有 Georges Franju 导演的冲突事实未触发更正。"),
    "dev_4478": ("UPDATE/答案粒度", "只记录 Stuart Burge 出生于 England，没有金标准 Essex；地点粒度过粗。"),
    "dev_4680": ("ANSWER", "四个出生/死亡日期均已绑定；Henry 约 74 岁、Nina 约 71 岁，ANSWER 却选 Nina，最终寿命计算错误。"),
    "dev_4784": ("UPDATE/答案粒度", "Taras Bulba 的导演出现两个候选，二者出生地都仅绑定为 Russia；未提取金标准城市 Moscow。"),
    "dev_4865": ("PLAN/UPDATE/ANSWER", "PLAN 不该包含最终比较 Q5；事实/绑定显示 1977 晚于 1951，Q5 却被一条直接答案事实绑定为 Fog And Sun，最终比较与日期相反。"),
    "dev_4961": ("MEMORY 协议", "Q1 先错绑 Singeetham Srinivasa Rao；后续出现 Alexander Korda 纠错候选，但 MEMORY_REPAIR 仍以 NONE 作为中间节点支持，触发 INFERRED_ONLY_FOR_INTERMEDIATE_QUERY，运行中止。"),
    "dev_5002": ("MEMORY/ANSWER", "Júdás 导演 Michael Curtiz 已找到且有 Hungarian 事实，国籍却被绑定为其本人；Wilby 的 American 已绑定，ANSWER 在第一侧国籍失真时答 no。"),
    "dev_5027": ("UPDATE", "父亲 Henry de Percy 第二代已绑定，仅记录死亡年份 1352，没有 Warkworth 死亡地点。"),
    "dev_5132": ("MEMORY", "已记录 Henry 是 William II 的父亲、George Louis 是 Henry 的父亲，足以形成两跳；但 Q1 错绑 William II 本人，Q2 没有正确实例化/绑定。"),
    "dev_5325": ("UPDATE/事实或标注冲突", "记录 Shabir 是 Indian，金标准为 Singapore；第二跳绑定并输出 Indian。现有轨迹不能仅凭结果判定是源文、抽取还是标注不一致。"),
    "dev_5343": ("MEMORY", "已抽到 Anne 是 Richard 的 child bride（配偶关系）及 Richard 的母亲 Elizabeth Woodville；Q1 对逆/间接婚姻表达返回 NOOP，导致第二跳不可达。"),
    "dev_5351": ("UPDATE/MEMORY/ANSWER", "只有 Leopold 是 Joseph 的父亲，没有 Leopold 的父亲 Ferdinand III；Q2 反而将 Joseph 错绑为祖父，ANSWER 又回退输出 Leopold。"),
    "dev_5416": ("答案语义/ANSWER", "Igor 被绑定 Russian-American、Nicolas 为 American；ANSWER 将复合国籍视为不相同并答 NO，金标准 yes 按共享 American 成分判定。"),
    "dev_5534": ("答案规范化", "抽取并回答 Annedal, Gothenburg, Sweden；金标准只有 Gothenburg/Göteborg。地点包含关系在 exact match 中未被承认。"),
    "dev_5885": ("UPDATE/证据冲突", "两片都先绑定 1990，后续又抽出 Sons of Ram 于 2012-11-02 上映；冲突未得到可靠消解，ANSWER 返回 UNKNOWN。"),
    "dev_6782": ("UPDATE", "导演 Alekos Sakellarios 已绑定，但墓地只记录‘未提及’，没有 First Cemetery of Athens。"),
    "dev_6785": ("答案语义/ANSWER", "两导演国籍分别 French、French-Senegalese；ANSWER 按字符串不完全相同答 no，金标准 yes 将复合国籍的 French 成分视为共有。"),
    "dev_6901": ("UPDATE/MEMORY/ANSWER", "父亲 Tancred 已找到，出生地节点却绑定其本人；唯一地名 Hauteville 属于 William 本人而不是父亲，ANSWER 跨主体借用为最终答案。"),
    "dev_7046": ("UPDATE", "母亲 Béatrice 已绑定，但未记录其母亲 Chantal，故祖母跳 UNKNOWN。"),
    "dev_7146": ("UPDATE/MEMORY/答案语义", "Des McAnuff 国籍记录为 American-Canadian，Q2 又被绑定为人名；ANSWER 输出复合国籍，金标准仅 American，包含关系未被规范化。"),
    "dev_7217": ("UPDATE/MEMORY/ANSWER", "只找到父亲 Oliba；Q2 错把父亲本人当祖父绑定，ANSWER 也输出 Oliba，缺 Bello 的上游事实。"),
    "dev_7221": ("ANSWER", "绑定日期 Miroslav Cikán 1962、Camillo Mastrocinque 1969；1962 更早，应选 Studujeme Za Školou，ANSWER 却选 Gli Eroi Del Doppio Gioco。"),
    "dev_8009": ("UPDATE/MEMORY/ANSWER", "Paasa 的 Cochin Haneefa 生日已绑定 1951，另一片导演有多个候选且第二侧生日未绑定；ANSWER 在比较条件不全时选 And The Heavens Above Us。"),
    "dev_8199": ("答案规范化", "回答 Lwów (Lviv), [Poland] (now Ukraine) 含金标准别名 Lwów/Lviv；exact match 因附加地区说明判错。"),
    "dev_8530": ("UPDATE/MEMORY/ANSWER", "只记录 Andrew II 是 Coloman 之父，没有其父 Béla III；Q2 把 Andrew II 本人绑定为祖父，最终错误。"),
    "dev_9193": ("MEMORY", "两名导演的具体生日已被 UPDATE 抽到（1890-11-06、1893-07-15），Q3/Q4 却绑定导演名字而非日期；ANSWER 因缺可比日期返回 UNKNOWN。"),
    "dev_9348": ("PLAN/UPDATE", "PLAN 直接问‘导演何时去世’，未拆导演身份节点；只记录 Bryan Forbes 的死亡日期、缺另一导演具体日期，无法比较。"),
    "dev_9618": ("UPDATE/MEMORY/ANSWER", "记录中已有‘Leopoldo Torre Nilsson 的父亲是 Leopoldo Torres Ríos’，但另存污染的循环事实；Q2 绑定 Nilsson 本人，ANSWER 没使用正确父子事实。"),
    "dev_9742": ("UPDATE/MEMORY/ANSWER", "Atheetham 错挂 Karel Kachyňa 为导演，Devan Nair 和 Karel 又各有冲突生日；成员分支污染后 ANSWER 选 Atheetham，不能可靠对应金标准影片。"),
    "dev_10047": ("UPDATE/MEMORY", "Ten9Eight 导演出现 Mary Mazzio 与 Hanro Smitsman 两个分支；其国籍节点被错绑为 Hanro 本人。对比证据不完整，ANSWER 返回 UNKNOWN；此题已用重测记录，非初跑 429。"),
    "dev_10191": ("UPDATE/事实错误", "Robert Rossen 已绑定，但死亡地点记录为 de Vienne 而非 Hollywood；错误事实经 MEMORY、ANSWER 逐层传递。"),
    "dev_10404": ("ANSWER", "绑定 Phil Rosen 死于 1951、Alex Smith 死于 1930；1930 更早，ANSWER 却选 Modern Mothers。"),
    "dev_10526": ("UPDATE/MEMORY", "已记录 Imagina 为 Adolf 的王后/配偶，却没绑定丈夫 Q1；死亡地点仅记录‘未提及’，缺 Göllheim。"),
    "dev_10872": ("答案规范化", "Young Australian of the Year Award 与标注 Young Australian of the Year 为同一奖项的加后缀写法；事实链完整，仅 exact match 判错。"),
    "dev_10994": ("PLAN 协议", "PLAN 添加了不应出现的最终比较 Q5；其 query 文本不含 ?birth_year 变量，却声明依赖 Q3,Q4，结构校验重试后仍失败，未进入 UPDATE。"),
    "dev_11141": ("PLAN/ANSWER", "把单一片名‘Chaplinesque, My Life And Hard Times’按逗号拆成两部电影，Q1/Q2 均问错对象；ANSWER 返回 Chaplinesque。"),
    "dev_11153": ("UPDATE/事实错误", "母亲 Helene 已绑定，但生日事实记录 1857-01-16 而金标准 1827-08-28；错误第二跳事实被直接回答。"),
    "dev_11285": ("PLAN", "PLAN 直接问两部电影导演的生日，未设置‘电影→导演’身份跳；UPDATE 仅记录 Michael Powell/Robert Tansey 的生日，无法路由到具体影片。"),
    "dev_11393": ("UPDATE/事实错误", "歌手 Lana Del Rey 已绑定，但出生地记录 Los Angeles，而金标准 New York；错误第二跳事实直接传给 ANSWER。"),
    "dev_11630": ("UPDATE/MEMORY", "Q2 问出生地点，UPDATE 给的是出生日期 1901-04-26；MEMORY_REPAIR 仍把日期绑到地点节点，ANSWER 因缺地点返回 UNKNOWN。"),
    "dev_11641": ("UPDATE/MEMORY/答案粒度", "丈夫 Emperor Daizong 已绑定；出生地节点被错绑为人名，ANSWER 从 China 事实输出国家，金标准为城市 Luoyang。"),
    "dev_11888": ("UPDATE/事实错误", "记录 Friedrich 的母亲为 Princess Adelheid，而标注为 Princess Louise Caroline；错误亲属事实经 MEMORY 绑定后输出。"),
    "dev_12150": ("UPDATE/MEMORY/ANSWER", "只给 Leslie 明确死亡日期 2016；Danny 无具体年份，Q3 死亡节点被错绑为导演人名。ANSWER 在无可靠双侧日期时选 Hot Rod Rumble。"),
    "dev_12309": ("答案规范化", "抽取与绑定支持 Lőcse，金标准别名只列 Levoča；两者可能是同一地点的不同历史/语言写法，需要对照原文或地名表核实，当前 exact match 判错。"),
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def load_rows(path: Path) -> dict[str, dict]:
    return {row["sample_id"]: row for row in map(json.loads, path.open(encoding="utf-8"))}


def trajectory_path(row: dict) -> Path:
    path = Path(row["trajectory_path"])
    return path if path.is_absolute() else ROOT / path


def plan_lines(trace: dict) -> list[str]:
    queries = trace.get("state", {}).get("plan", {}).get("queries", [])
    if queries:
        result = []
        for query in queries:
            parents = ", ".join(dict.fromkeys(query.get("inputs", {}).values())) or "NONE"
            result.append(f"{query['id']}  {query['template']}  → {query.get('output', '')}  [depends_on: {parents}]")
        return result
    calls = [call for call in trace.get("model_calls", []) if call.get("interface") == "PLAN"]
    if calls:
        return [str(calls[-1].get("parsed_output") or calls[-1].get("raw_response") or "PLAN 无可解析输出")]
    return ["PLAN 未生成"]


def main() -> None:
    base = load_rows(BASE)
    retry = load_rows(RETRY)
    assert len(base) == 128 and len(retry) == 21
    # Never inherit a stale error/status from the original attempt when the
    # retry record uses a narrower schema.
    merged = {sid: retry[sid] if sid in retry else row for sid, row in base.items()}
    wrong = {sid: row for sid, row in merged.items() if not row["answer_exact"]}
    assert len(wrong) == 69 and set(wrong) == set(DIAGNOSES)
    with QUESTIONS.open(encoding="utf-8-sig", newline="") as stream:
        question_map = {row["benchmark_id"]: row["question"] for row in csv.DictReader(stream)}

    stage_counts = Counter(DIAGNOSES[sid][0].split("/")[0] for sid in wrong)
    cards = []
    for sid in sorted(wrong, key=lambda item: int(item.split("_")[1])):
        row = wrong[sid]
        trace = json.loads(trajectory_path(row).read_text(encoding="utf-8"))
        state = trace.get("state", {})
        stage, diagnosis = DIAGNOSES[sid]
        gold = row["gold_answers"]
        prediction = row.get("prediction")
        if prediction is None:
            prediction = "未给出（UNKNOWN）" if row.get("runtime_status") == "INSUFFICIENT" else "未给出（运行失败）"
        facts = list(state.get("facts", {}).values())
        fact_lines = [f"{str(fact.get('fact_id', ''))[:10]}: {fact.get('text', '')}" for fact in facts]
        bindings = state.get("bindings", {})
        source = "21 题重测" if sid in retry else "原始 128 题"
        aliases = "；其他金标准别名：" + "、".join(map(str, gold[1:])) if len(gold) > 1 else ""
        error = row.get("error") or "; ".join(row.get("reason_codes") or [])
        err_html = f'<div class="error"><b>错误码：</b>{esc(error)}</div>' if error else ""
        cards.append(f'''<article class="case" id="{esc(sid)}" data-stage="{esc(stage)}" data-source="{esc(source)}" data-search="{esc(' '.join([sid, question_map[sid], stage, diagnosis, str(gold[0]), str(prediction)]).lower())}">
  <div class="case-head"><h2>{esc(sid)}</h2><span class="badge">{esc(stage)}</span><span class="badge light">{esc(source)}</span><span class="badge light">{esc(row.get('runtime_status'))}</span></div>
  <p><b>问题：</b>{esc(question_map[sid])}</p>
  <div class="answers"><div><b>正确答案：</b>{esc(gold[0])}<span class="muted">{esc(aliases)}</span></div><div><b>错误回答：</b>{esc(prediction)}</div></div>
  <div class="cause"><b>出错原因与环节：</b>{esc(diagnosis)}</div>{err_html}
  <details><summary>Query Plan（实际运行/最终被拒的模型输出）</summary><pre>{esc(chr(10).join(plan_lines(trace)))}</pre></details>
  <details><summary>工作记忆证据（绑定与抽取事实）</summary><pre>绑定：{esc(json.dumps(bindings, ensure_ascii=False, indent=2))}\n\n事实：\n{esc(chr(10).join(fact_lines) if fact_lines else '无')}</pre></details>
</article>''')

    count_text = "、".join(f"{esc(k)} {v}" for k, v in stage_counts.most_common())
    output = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DelayBind R2：128 题合并结果的 69 道错题分析</title>
<style>
:root{{--bg:#f5f7fb;--ink:#16202c;--muted:#627084;--line:#dce3ed;--accent:#2356a4}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}header{{background:#152c4a;color:#fff;padding:38px max(20px,calc((100vw - 1120px)/2)) 30px}}h1{{font-size:28px;margin:0 0 10px}}header p{{margin:4px 0;color:#e2ebf7}}main{{max-width:1120px;margin:auto;padding:24px 20px 70px}}.overview,.case{{background:white;border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 9px #253c5910}}.overview{{padding:18px 22px;margin-bottom:16px}}.overview p{{margin:7px 0}}.controls{{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}}input,select{{font:inherit;border:1px solid #b7c5d9;border-radius:7px;padding:9px 11px;background:#fff;min-width:170px}}input{{flex:1}}.case{{padding:18px 22px;margin:14px 0}}.case-head{{display:flex;align-items:center;gap:7px;flex-wrap:wrap}}h2{{font-size:19px;margin:0 8px 0 0}}.badge{{font-size:12px;background:#dbe9ff;color:#17447b;border-radius:100px;padding:3px 9px;font-weight:600}}.badge.light{{background:#edf1f6;color:#53637a}}.case p{{margin:12px 0}}.answers{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}}.answers>div{{background:#f6f8fc;padding:10px 12px;border-radius:7px}}.muted{{font-size:12px;color:var(--muted)}}.cause{{border-left:3px solid var(--accent);padding:9px 12px;background:#f2f6fd;margin:12px 0}}.error{{color:#9a2c2c;font-size:13px;margin:7px 0}}details{{border-top:1px solid var(--line);padding:9px 0}}summary{{cursor:pointer;color:var(--accent);font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:#f5f7fa;padding:12px;border-radius:7px}}a{{color:#2356a4}}@media(max-width:650px){{.answers{{grid-template-columns:1fr}}h1{{font-size:23px}}}}@media print{{header{{background:white;color:black;padding:10px}}header p{{color:black}}.controls{{display:none}}.case{{break-inside:avoid;box-shadow:none}}details{{display:block}}details>pre{{display:block}}}}
</style></head><body><header><h1>DelayBind R2 · 128 题错题逐题诊断</h1>
<p>用 21 道 API 受影响题的重测结果覆盖原记录；其余 107 道沿用原始结果。</p>
<p>合并结果：59 / 128 题 exact match 正确（46.1%），69 题未匹配。错题中 42 题给出错误答案、25 题 UNKNOWN、2 题协议/运行错误。</p></header>
<main><section class="overview"><b>阅读说明</b><p>“正确答案”显示 benchmark 首个标准写法，其他别名随其后；“错误回答”为本次采用记录的实际 prediction。标注为“答案规范化”的题目未必是知识推理错误，需与真正事实/绑定/比较错误分开。</p>
<p>“未抽到”“缺少”均指本题最终 trajectory 的事实/绑定状态；不自动推断原文完全不存在。错误环节按可观察的首要故障标注，多个环节共同作用时并列。每道题可展开实际 Query Plan、最终绑定与抽取事实核验。</p>
<p>首要故障类别概览：{count_text}。实验日志保存在 <code>results/v52-r2-full128-member-graph-text22-20260923/</code> 与 <code>results/v52-r2-full128-member-graph-text22-retry5-c2-20260923/</code>。</p></section>
<div class="controls"><input id="search" type="search" placeholder="搜索题号、问题、答案或原因"><select id="stage"><option value="">全部环节</option>{''.join(f'<option value="{esc(k)}">{esc(k)}</option>' for k in sorted({v[0] for v in DIAGNOSES.values()}))}</select><select id="source"><option value="">全部来源</option><option>21 题重测</option><option>原始 128 题</option></select><span id="visible"></span></div>
{''.join(cards)}</main>
<script>const cases=[...document.querySelectorAll('.case')],q=document.getElementById('search'),st=document.getElementById('stage'),so=document.getElementById('source'),v=document.getElementById('visible');function filter(){{let n=0;for(const c of cases){{const show=c.dataset.search.includes(q.value.trim().toLowerCase())&&(!st.value||c.dataset.stage===st.value)&&(!so.value||c.dataset.source===so.value);c.hidden=!show;if(show)n++}}v.textContent=`显示 ${{n}} / ${{cases.length}} 题`}}q.addEventListener('input',filter);st.addEventListener('change',filter);so.addEventListener('change',filter);filter();</script></body></html>'''
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(output, encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(cards)} cases; stages: {stage_counts}")


if __name__ == "__main__":
    main()
