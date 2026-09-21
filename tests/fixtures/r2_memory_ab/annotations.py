"""Visible-input labels, fixed before any A/B API output is observed.

No final-answer gold is used. In particular, dev_91 must follow its visible
employment fact, dev_954 must obey SINGLE, and son-of alone is not fatherhood.
"""

def bound(value, support, reason="唯一适用事实直接回答当前子问题。"):
    return dict(decision="BOUND", value=value, support_aliases=support, reason=reason)


def noop(reason):
    return dict(decision="NOOP", value=None, support_aliases=[], reason=reason)


LABELS = {
    "dev_7920.Q1.1": noop("只有 father 事实，不能回答 husband。"),
    "dev_7920.Q1.2": bound("Frederik, Crown Prince of Denmark", ["F2"], "F1 为错关系；F2 明确回答 husband，出生日期是否齐全不影响本次绑定。"),
    "dev_4961.Q1.1": noop("唯一事实属于 Fanny and Alexander，固定电影不匹配 Yamata。"),
    "dev_4961.Q1.2": bound("Alexander Korda", ["F2"], "忽略 F1 的错误电影；F2 直接给出 Yamata 导演，不能要求同时提供死亡地点。"),
    "dev_10616.Q1.1": noop("son 描述孩子；可见事实只确定 Jobst 为父母之一，缺少父亲角色或性别依据。不以金标补证据。"),
    "dev_4784.Q1.1": noop("同一事实给出两个导演，SINGLE 无法唯一选取；复合事实不是忽略多值的理由。"),
    "dev_954.Q1.1": noop("Chinese 和 German 均适用，当前 SINGLE 合同不容许任意舍弃其一。"),
    "dev_954.Q2.1": bound("German", ["F1"]),
    "dev_11393.Q1.1": bound("Lana Del Rey", ["F1"]),
    "dev_11393.Q2.1": bound("New York City", ["F1"]),
    "dev_5343.Q1.1": bound("Richard of Shrewsbury, 1st Duke of York", ["F1"]),
    "dev_5343.Q2.1": bound("Elizabeth Woodville", ["F1"]),
    "dev_91.Q1.1": bound("Thorvald Stoltenberg", ["F1"]),
    "dev_91.Q2.1": bound("the Norwegian government", ["F1"], "这是本次可见事实支持的工作机构；不能凭最终金标补入 United Nations。"),
}

# Each row: wrong entity, wrong relation for the SAME target entity,
# equivalent wording of the supporting fact, direct counterevidence.
# Fictional noise appears only in offline perturbations, never production prompts.
PERTURBATIONS = {
    "dev_7920.Q1.2": (
        "Owen Vale is the husband of Mira Vale.",
        "Elias Vale is the father of Mary, Crown Princess of Denmark.",
        "Mary, Crown Princess of Denmark's husband is Frederik, Crown Prince of Denmark.",
        "Frederik, Crown Prince of Denmark is not the husband of Mary, Crown Princess of Denmark.",
    ),
    "dev_4961.Q1.2": (
        "Film Lantern was directed by Adrian Vale.",
        "Film Yamata was produced by Adrian Vale.",
        "Film Yamata was directed by Alexander Korda.",
        "Alexander Korda did not direct film Yamata.",
    ),
    "dev_11393.Q1.1": (
        "The performer of song Lantern Echo is Adrian Vale.",
        "Adrian Vale wrote song Stargirl Interlude.",
        "Song Stargirl Interlude is performed by Lana Del Rey.",
        "Lana Del Rey is not the performer of song Stargirl Interlude.",
    ),
    "dev_11393.Q2.1": (
        "Adrian Vale was born in Willow Bay.",
        "Lana Del Rey works in Willow Bay.",
        "New York City is Lana Del Rey's birthplace.",
        "Lana Del Rey was not born in New York City.",
    ),
    "dev_5343.Q1.1": (
        "Adrian Vale is the husband of Mira Vale.",
        "Anne De Mowbray, 8Th Countess Of Norfolk's father is Adrian Vale.",
        "Richard of Shrewsbury, 1st Duke of York is the husband of Anne De Mowbray, 8Th Countess Of Norfolk.",
        "Richard of Shrewsbury, 1st Duke of York is not the husband of Anne De Mowbray, 8Th Countess Of Norfolk.",
    ),
    "dev_5343.Q2.1": (
        "Adrian Vale's mother is Mira Vale.",
        "Richard of Shrewsbury, 1st Duke of York's teacher is Mira Vale.",
        "Elizabeth Woodville is the mother of Richard of Shrewsbury, 1st Duke of York.",
        "Elizabeth Woodville is not the mother of Richard of Shrewsbury, 1st Duke of York.",
    ),
    "dev_954.Q2.1": (
        "Adrian Vale is Canadian.",
        "Johann Christian Gustav Lucae works in Canada.",
        "Johann Christian Gustav Lucae holds German nationality.",
        "Johann Christian Gustav Lucae does not hold German nationality.",
    ),
    "dev_91.Q1.1": (
        "Adrian Vale is the husband of Mira Vale.",
        "Karin Stoltenberg's father is Adrian Vale.",
        "Thorvald Stoltenberg is the husband of Karin Stoltenberg.",
        "Thorvald Stoltenberg is not the husband of Karin Stoltenberg.",
    ),
    "dev_91.Q2.1": (
        "Adrian Vale works at Willow University.",
        "Thorvald Stoltenberg was educated at Willow University.",
        "The Norwegian government employs Thorvald Stoltenberg.",
        "Thorvald Stoltenberg does not work at the Norwegian government.",
    ),
}
