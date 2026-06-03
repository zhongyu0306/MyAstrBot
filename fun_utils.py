from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import random
from typing import Any, Sequence

from astrbot.api.event import AstrMessageEvent


_HELP_TOKENS = {"帮助", "help", "h", "说明"}

_PERSONA_PREFIXES = (
    "嘴硬心软的",
    "表面摸鱼但偷偷发力的",
    "气氛拿捏很稳的",
    "随时能反杀 deadline 的",
    "今天有点超模的",
    "精神状态领先版本的",
    "明明很困但还挺能打的",
    "看起来很佛其实很卷的",
    "容易被低估的",
    "一出手就自带 BGM 的",
)
_PERSONA_ROLES = (
    "会议刺客",
    "拖延终结者",
    "情绪修补匠",
    "计划流魔法师",
    "灵感搬运工",
    "临场救火队长",
    "脑洞策展人",
    "社交破冰员",
    "日常小宇宙驾驶员",
    "剧情推进型主角",
)
_PERSONA_AURAS = (
    "表面平静，内心已经把节奏盘明白了",
    "适合做第一个开口的人，也适合做最后拍板的人",
    "今天容易靠松弛感赢，而不是靠硬撑赢",
    "你不一定最吵，但大概率是那个最有存在感的人",
    "越到关键时刻越像是你的主场",
    "适合把复杂的事说得很清楚",
    "适合用幽默感化解尴尬和僵局",
    "今天的你很适合做队伍里的定海神针",
)
_PERSONA_BUFFS = (
    "被动技能：临时加班不一定快乐，但你今天的临场发挥会超预期",
    "被动技能：容易在聊天里一句话点醒别人",
    "被动技能：今天适合推进搁置很久的小任务",
    "被动技能：社交运偏高，发消息更容易收到正反馈",
    "被动技能：适合把碎片灵感整理成真正能落地的东西",
    "被动技能：很会把烂局救到及格以上",
    "被动技能：今天说服力在线，适合提需求和争取资源",
    "被动技能：容易在不经意的时候收获意外助攻",
)
_PERSONA_NERFS = (
    "弱点提示：容易高估自己的续航，记得按时补水和吃饭",
    "弱点提示：别因为嘴硬把“我需要帮忙”憋回去",
    "弱点提示：今天不适合一口气给自己加太多支线任务",
    "弱点提示：别在情绪上头时做最终决定",
    "弱点提示：容易突然想摆烂，但你只需要先动 10 分钟",
    "弱点提示：注意别把别人一句玩笑话放大成压力",
    "弱点提示：今天和拖延只隔一张床和一个沙发的距离",
    "弱点提示：别为了礼貌答应过多额外请求",
)
_PERSONA_QUESTS = (
    "主线任务：完成一件你已经拖了三天以上的小事",
    "主线任务：把一个模糊想法讲清楚给别人听",
    "主线任务：主动推进一次你原本想再等等的沟通",
    "主线任务：今天至少认真夸一个人一次",
    "主线任务：把手头最乱的一块收拾到可继续推进",
    "主线任务：把自己从“等状态好再做”切到“先做一点”",
    "主线任务：找回一点对生活的控制感，哪怕只是一顿饭",
    "主线任务：今天做一个不那么完美但真实的表达",
)
_PERSONA_LINES = (
    "出场台词：我不一定准备好了，但我可以边上场边变强。",
    "出场台词：今天不求全赢，但求每一步都算数。",
    "出场台词：要是世界不给我台阶，我就自己搭一个。",
    "出场台词：先动手，后完美，这是今天的版本答案。",
    "出场台词：人可以发呆，但事情最好别一直卡着。",
    "出场台词：不怕慢，怕的是脑内彩排一百遍还没开始。",
    "出场台词：今天的好运，往往藏在你主动那一下里。",
    "出场台词：我允许自己普通，但不允许自己一直原地打转。",
)
_PERSONA_SIDEKICKS = (
    "最佳拍档：一杯提神饮料和一个愿意秒回你的搭子",
    "最佳拍档：耳机、待办清单和刚刚好的独处时间",
    "最佳拍档：一个能接你梗的人",
    "最佳拍档：热乎乎的饭和清晰的优先级",
    "最佳拍档：一个小目标和一点点胜负欲",
    "最佳拍档：能让你快速进入状态的 BGM",
    "最佳拍档：一个可以先做出来再优化的草稿",
    "最佳拍档：把你从床上或沙发上拽起来的理由",
)

_MEAL_KEYWORDS = (
    ("早餐", ("早餐", "早饭", "早点", "晨间")),
    ("午饭", ("午饭", "午餐", "中饭", "中餐")),
    ("晚饭", ("晚饭", "晚餐")),
    ("夜宵", ("夜宵", "宵夜")),
    ("下午茶", ("下午茶", "奶茶", "甜点", "咖啡")),
)
_STYLE_KEYWORDS = (
    ("减脂", ("减脂", "轻食", "健身", "低卡", "控卡")),
    ("清淡", ("清淡", "养胃", "淡口", "不油")),
    ("重口", ("重口", "下饭", "辣", "刺激", "香辣")),
    ("热乎", ("热乎", "热的", "暖和", "汤", "热汤")),
    ("省钱", ("省钱", "便宜", "穷", "预算", "划算")),
    ("一人食", ("一个人", "独自", "一人食", "单人", "自己吃")),
    ("朋友局", ("聚餐", "朋友", "聚会", "多人", "一起吃")),
    ("尝鲜", ("新奇", "尝鲜", "整活", "没吃过", "刺激点")),
)
_FOOD_ITEMS = (
    {"name": "鸡蛋三明治 + 拿铁", "meals": {"早餐"}, "tags": {"经典", "一人食"}},
    {"name": "豆浆油条 + 茶叶蛋", "meals": {"早餐"}, "tags": {"经典", "省钱"}},
    {"name": "皮蛋瘦肉粥 + 小笼包", "meals": {"早餐"}, "tags": {"热乎", "清淡"}},
    {"name": "手抓饼 + 豆浆", "meals": {"早餐"}, "tags": {"经典", "省钱"}},
    {"name": "燕麦酸奶水果碗", "meals": {"早餐", "下午茶"}, "tags": {"减脂", "清淡"}},
    {"name": "牛角包 + 美式咖啡", "meals": {"早餐", "下午茶"}, "tags": {"一人食"}},
    {"name": "煎饺 + 热豆浆", "meals": {"早餐"}, "tags": {"热乎", "经典"}},
    {"name": "热干面 + 卤蛋", "meals": {"早餐", "午饭"}, "tags": {"重口", "经典"}},
    {"name": "照烧鸡腿饭", "meals": {"午饭", "晚饭"}, "tags": {"经典", "一人食"}},
    {"name": "番茄牛腩面", "meals": {"午饭", "晚饭"}, "tags": {"热乎", "一人食"}},
    {"name": "轻食鸡肉沙拉", "meals": {"午饭", "晚饭"}, "tags": {"减脂", "清淡", "一人食"}},
    {"name": "虾仁滑蛋饭", "meals": {"午饭", "晚饭"}, "tags": {"清淡", "一人食"}},
    {"name": "咖喱鸡饭", "meals": {"午饭", "晚饭"}, "tags": {"经典", "热乎"}},
    {"name": "石锅拌饭", "meals": {"午饭", "晚饭"}, "tags": {"热乎", "尝鲜"}},
    {"name": "麻辣香锅", "meals": {"午饭", "晚饭", "夜宵"}, "tags": {"重口", "朋友局"}},
    {"name": "黄焖鸡米饭", "meals": {"午饭", "晚饭"}, "tags": {"热乎", "经典"}},
    {"name": "荞麦冷面 + 溏心蛋", "meals": {"午饭", "晚饭"}, "tags": {"减脂", "清淡", "尝鲜"}},
    {"name": "烤肉拌饭", "meals": {"午饭", "晚饭"}, "tags": {"重口", "朋友局"}},
    {"name": "寿喜锅", "meals": {"午饭", "晚饭"}, "tags": {"热乎", "朋友局", "尝鲜"}},
    {"name": "水饺 + 紫菜蛋花汤", "meals": {"午饭", "晚饭"}, "tags": {"清淡", "热乎"}},
    {"name": "酸菜鱼", "meals": {"午饭", "晚饭"}, "tags": {"重口", "朋友局"}},
    {"name": "番茄肥牛乌冬面", "meals": {"午饭", "晚饭"}, "tags": {"热乎", "经典"}},
    {"name": "鸡胸肉藜麦能量碗", "meals": {"午饭", "晚饭"}, "tags": {"减脂", "清淡"}},
    {"name": "烤鸡腿便当", "meals": {"午饭", "晚饭"}, "tags": {"省钱", "经典"}},
    {"name": "肉末茄子盖饭", "meals": {"午饭", "晚饭"}, "tags": {"重口", "经典"}},
    {"name": "越南河粉", "meals": {"午饭", "晚饭"}, "tags": {"清淡", "尝鲜"}},
    {"name": "小龙虾拌面", "meals": {"晚饭", "夜宵"}, "tags": {"重口", "尝鲜"}},
    {"name": "炸鸡 + 薯条", "meals": {"晚饭", "夜宵"}, "tags": {"重口", "朋友局"}},
    {"name": "关东煮", "meals": {"晚饭", "夜宵"}, "tags": {"热乎", "清淡"}},
    {"name": "烤冷面", "meals": {"夜宵"}, "tags": {"重口", "省钱"}},
    {"name": "炒河粉", "meals": {"夜宵"}, "tags": {"经典", "省钱"}},
    {"name": "烧烤拼盘", "meals": {"夜宵"}, "tags": {"重口", "朋友局"}},
    {"name": "泡面加蛋加肠", "meals": {"夜宵"}, "tags": {"省钱", "一人食"}},
    {"name": "小馄饨 + 葱油拌面", "meals": {"夜宵"}, "tags": {"热乎", "一人食"}},
    {"name": "卤味拼盘", "meals": {"夜宵"}, "tags": {"重口", "一人食"}},
    {"name": "可颂 + 冰美式", "meals": {"下午茶"}, "tags": {"经典", "一人食"}},
    {"name": "巴斯克蛋糕 + 拿铁", "meals": {"下午茶"}, "tags": {"朋友局"}},
    {"name": "抹茶千层 + 热茶", "meals": {"下午茶"}, "tags": {"清淡", "尝鲜"}},
    {"name": "铜锣烧 + 牛奶", "meals": {"下午茶"}, "tags": {"省钱", "经典"}},
    {"name": "酸奶杯 + 水果", "meals": {"下午茶"}, "tags": {"减脂", "清淡"}},
    {"name": "珍珠奶茶 + 小蛋挞", "meals": {"下午茶"}, "tags": {"重口", "朋友局"}},
)
_MEAL_DRINKS = {
    "早餐": ("热豆浆", "拿铁", "美式咖啡", "热牛奶", "乌龙茶"),
    "午饭": ("无糖乌龙", "柠檬水", "冰美式", "气泡水", "酸梅汤"),
    "晚饭": ("桂花乌龙", "柠檬茶", "可乐", "苏打水", "冰红茶"),
    "夜宵": ("冰可乐", "无糖气泡水", "乌梅汁", "酸奶", "热茶"),
    "下午茶": ("拿铁", "伯爵红茶", "生椰拿铁", "抹茶拿铁", "气泡美式"),
}
_STYLE_DRINKS = {
    "减脂": ("无糖乌龙", "黑咖啡", "气泡水", "冷泡茶"),
    "清淡": ("热茶", "柠檬水", "大麦茶"),
    "重口": ("酸梅汤", "冰可乐", "气泡水"),
    "热乎": ("热豆浆", "味噌汤", "热柠檬水"),
    "省钱": ("白开水", "自带茶包", "便利店无糖茶"),
    "朋友局": ("大瓶快乐水", "果茶", "苏打水"),
    "尝鲜": ("柚子气泡饮", "青提冷泡茶", "梅子茶"),
}
_MEAL_TIPS = {
    "早餐": (
        "别为了省时间只喝咖啡空腹顶着上班。",
        "早餐今天更适合热乎一点，胃会谢谢你。",
        "先吃点东西再出门，情绪稳定度会高很多。",
    ),
    "午饭": (
        "中午别点等 50 分钟的店，你饿的时候会看谁都像对手。",
        "如果下午还要干活，油别太猛，困意会来得很真诚。",
        "今天的午饭重点是吃到嘴，不是研究半小时。 ",
    ),
    "晚饭": (
        "晚饭别拖太晚，拖着拖着就会开始乱点。",
        "如果今天很累，优先选热的和容易满足的。",
        "晚饭更适合让自己恢复，不适合证明意志力。",
    ),
    "夜宵": (
        "夜宵最怕越点越多，先决定分量再下单。",
        "深夜嘴馋可以有，但别把明天的胃一起透支了。",
        "夜宵更适合小满足，不适合大型放纵。",
    ),
    "下午茶": (
        "下午茶重点是回血，不是把自己糖分拉爆。",
        "今天的下午茶适合刚刚好，不适合喝完心虚。",
        "一口甜的可以，但别让它替代正餐。",
    ),
}
_STYLE_TIPS = {
    "减脂": (
        "优先选有蛋白质的，不然一小时后还是会想加餐。",
        "低卡不等于没滋味，别把自己吃得像在受罚。",
    ),
    "清淡": (
        "今天适合吃完舒服，不适合吃完躺平。",
        "清淡路线更看重“热”和“新鲜”，不是单纯没味道。",
    ),
    "重口": (
        "辣和咸都能提神，但也容易让你事后疯狂找水。",
        "重口可以有，记得配点清口的饮品救场。",
    ),
    "省钱": (
        "省钱不是凑合，重点是把性价比拉满。",
        "预算紧的时候，优先选稳定好吃的经典项。",
    ),
    "一人食": (
        "一个人吃也值得认真一点，不需要总拿“随便”打发自己。",
        "一人食今天更适合简单、快、能让人心情稳住的。",
    ),
    "朋友局": (
        "多人局最好选容错高的，不然大家会一起选择困难。",
        "朋友局今天适合边聊边吃，别搞太复杂的流程。",
    ),
    "尝鲜": (
        "想整活可以，但别在特别饿的时候拿自己冒险。",
        "尝鲜局最怕翻车，留一个稳妥备选更安心。",
    ),
}
_LAZY_PLANS = {
    "早餐": (
        "偷懒方案：便利店拿个三明治加热饮，也比空腹硬扛强。",
        "偷懒方案：楼下包子铺直接解决，先把血条补上。",
    ),
    "午饭": (
        "偷懒方案：直接选最近那家稳定不踩雷的便当。",
        "偷懒方案：就近找有热菜和主食的店，效率优先。",
    ),
    "晚饭": (
        "偷懒方案：今天别折磨自己，选最熟的那家热饭热面。",
        "偷懒方案：点一个主食 + 一个热汤，恢复比新鲜感重要。",
    ),
    "夜宵": (
        "偷懒方案：控制在一份主食或一份小吃内，别从“垫一下”吃成夜市专场。",
        "偷懒方案：关东煮或小馄饨就够了，别在深夜挑战胃。",
    ),
    "下午茶": (
        "偷懒方案：一杯喝的加一个小甜点，刚好把电量补回来。",
        "偷懒方案：如果只是想提神，直接来杯咖啡别假装自己饿了。",
    ),
}

_SIGNS = (
    {"level": "上上签", "title": "云开月明", "score": 2, "line": "卡住的地方会慢慢亮起来，别急着怀疑自己。"},
    {"level": "上签", "title": "借风成帆", "score": 1, "line": "今天比起单打独斗，更适合借力推进。"},
    {"level": "上签", "title": "水到渠成", "score": 1, "line": "不用把每一步都演到完美，关键节点自然会接上。"},
    {"level": "中上签", "title": "先抑后扬", "score": 1, "line": "前半段容易发闷，后半段会逐渐顺起来。"},
    {"level": "中签", "title": "稳字当头", "score": 0, "line": "今天不是爆发日，更像是把局面稳稳托住的一天。"},
    {"level": "中签", "title": "绕路成景", "score": 0, "line": "绕一点不算白走，有些答案本来就藏在侧面。"},
    {"level": "小吉签", "title": "人和最贵", "score": 0, "line": "今天很多事靠沟通和气氛，比靠硬碰硬更有用。"},
    {"level": "平签", "title": "慢火出香", "score": 0, "line": "看起来慢一点，但这种慢反而更容易把结果做扎实。"},
    {"level": "小凶签", "title": "先收再放", "score": -1, "line": "先把风险和情绪收住，再谈下一步会更稳。"},
    {"level": "小凶签", "title": "雾里看花", "score": -1, "line": "眼前最诱人的那部分，不一定是全貌。"},
    {"level": "下签", "title": "莫逞强", "score": -2, "line": "硬扛只会把小问题拖成大问题，先止损更体面。"},
    {"level": "下下签", "title": "暂缓为上", "score": -2, "line": "今天最大的勇气，可能是愿意按下暂停。"},
)
_DRAW_DOMAIN_KEYWORDS = (
    ("工作", ("工作", "项目", "offer", "面试", "汇报", "领导", "副业", "跳槽", "客户", "合作")),
    ("感情", ("感情", "恋爱", "喜欢", "对象", "表白", "复合", "分手", "约会")),
    ("学业", ("学习", "考试", "考研", "论文", "作业", "答辩", "上岸", "背书")),
    ("钱财", ("钱", "买", "卖", "投资", "基金", "股票", "理财", "消费", "花钱", "赚钱")),
    ("选择", ("要不要", "值不值", "适不适合", "应不应该", "能不能", "去不去")),
)
_DRAW_OPENERS = {
    "工作": (
        "这件事更像节奏题，不像蛮力题。",
        "工作上的这签，重点不在“冲多猛”，而在“有没有对准”。",
        "你现在要处理的，不只是能力问题，还有时机和边界。 ",
    ),
    "感情": (
        "感情这题今天不适合全靠猜。",
        "这支签更提醒你把真实情绪讲清楚，而不是继续脑补。",
        "关系里的很多卡点，不一定是没感情，可能只是没说透。",
    ),
    "学业": (
        "学习这题更像复利，不像刮彩票。",
        "今天的学业签不看一时状态，更看你能不能稳住节奏。",
        "别拿一次情绪波动去否定整段积累。",
    ),
    "钱财": (
        "钱相关的问题，今天更看边界感。",
        "这支签不是单纯劝你冲或缩，而是提醒你先看代价。",
        "涉及钱的时候，清醒比兴奋值钱得多。",
    ),
    "选择": (
        "你问的是选择题，而这签更像在提醒你看隐藏成本。",
        "这类问题不只是“喜不喜欢”，还要看你能不能长期接住。",
        "今天的重点不是马上拍板，而是先看哪条路更适合现在的你。",
    ),
    "通用": (
        "今天的签更像方向提示，不是绝对判决。",
        "这件事未必只有是和否两种答案，节奏本身也很重要。",
        "你现在需要的，可能不是更强的运气，而是更清楚的动作。",
    ),
}
_DRAW_ANALYSIS = {
    "positive": (
        "整体偏向可推进，但更适合稳着发力，不用急着一口吃成。",
        "局面是偏顺的，你只要别自己先乱，路会越走越清楚。",
        "今天的风向对你不坏，主动一点通常比继续观望更有回报。",
    ),
    "neutral": (
        "结果不会凭空掉下来，但你做的每一步都在慢慢累积手感。",
        "这更像一局运营盘，别急着要立刻的爆点。",
        "不是不能做，而是更适合边走边校准。",
    ),
    "caution": (
        "今天更适合先收口，再决定要不要扩大动作。",
        "如果你已经感觉到不对劲，这支签建议你认真对待那个直觉。",
        "先把损耗降下来，再谈推进，反而能保住后面的空间。",
    ),
}
_DRAW_ACTIONS = {
    "工作": {
        "positive": ("先把关键人拉进来同步一下。", "主动推进一次你原本还在犹豫的沟通。", "把想法写成简版方案再去聊。 "),
        "neutral": ("先补一块最影响结果的短板。", "把需求、时间和边界重新对齐一次。", "拆成两步做，先拿到一个小反馈。 "),
        "caution": ("暂时别替别人扛全部责任。", "先确认资源和风险，再决定是否接下。", "把口头约定变成可追踪的文字。 "),
    },
    "感情": {
        "positive": ("可以更坦白一点表达在意。", "适合发出一个轻松但真诚的邀约。", "把好感落到具体行动上。 "),
        "neutral": ("先确认你自己真正想要什么关系。", "把期待讲清楚，比反复试探更有效。", "先观察对方是否有稳定回应。 "),
        "caution": ("别在上头时追着要答案。", "先把情绪放平，再决定要不要继续投入。", "不要只看一时热度，先看长期一致性。 "),
    },
    "学业": {
        "positive": ("适合啃一块之前不想碰的难点。", "做一轮限时输出，效率会比你预想的高。", "去交一次作业、报名或预约，把流程推进起来。 "),
        "neutral": ("今天更适合回归基础和计划。", "先把知识点归类，再做题。", "别追求学很久，先追求学得真。 "),
        "caution": ("别拿焦虑感冒充努力。", "先停下来整理错因，比盲刷更重要。", "如果很累，休息半小时再回来会更值。 "),
    },
    "钱财": {
        "positive": ("适合做信息整理，不适合一把梭。", "先把预算和底线写下来。", "可以推进，但记得给自己留余地。 "),
        "neutral": ("今天更适合比较和核算，不适合冲动下单。", "先看长期成本，不要只看眼前优惠。", "把“想要”和“需要”分开再决定。 "),
        "caution": ("先别被限时和气氛带着走。", "如果你自己都没看懂，就先不下决定。", "先缩小金额和风险敞口。 "),
    },
    "选择": {
        "positive": ("可以往更想要的那边再靠一步。", "试着先做一个低成本试运行。", "今天适合给自己一个明确答复。 "),
        "neutral": ("先列出你最在意的两个标准。", "允许自己暂时不选最完美，只选现阶段最合适。", "先排除最不适合的，再看剩下的。 "),
        "caution": ("别因为怕后悔就急着选。", "如果代价还没看清，先缓一缓。", "先把最糟结果想明白，再决定值不值。 "),
    },
    "通用": {
        "positive": ("主动一点通常比等运气更有效。", "今天适合把想法落成一个具体动作。", "你可以比平时更相信自己的判断一点。 "),
        "neutral": ("把事情往前推一小步就够了。", "先把手上最卡的一点疏通。", "不要急，节奏对了结果自然跟上。 "),
        "caution": ("先停一下，确认自己不是在被情绪推着走。", "今天更适合保守迭代，不适合大跳步。", "把复杂问题拆小，会比硬顶着舒服很多。 "),
    },
}
_DRAW_TABOOS = {
    "工作": {
        "positive": ("忌：明明能借力却非要单扛。", "忌：因为顺一点就忽略细节。"),
        "neutral": ("忌：把所有任务都当成同一优先级。", "忌：没有确认边界就直接开干。"),
        "caution": ("忌：情绪化接锅。", "忌：在信息不全时强行拍板。"),
    },
    "感情": {
        "positive": ("忌：只暗示不表达。", "忌：把轻松聊天聊成压迫审问。"),
        "neutral": ("忌：拿脑补代替沟通。", "忌：急着定义关系。"),
        "caution": ("忌：靠拉扯验证在不在乎。", "忌：把沉默理解成你想听的答案。"),
    },
    "学业": {
        "positive": ("忌：状态一好就疯狂加码。", "忌：只收藏资料不真正动笔。"),
        "neutral": ("忌：计划写得很满，执行又太散。", "忌：因为一题卡住就怀疑自己。"),
        "caution": ("忌：用熬夜补偿焦虑。", "忌：把休息看成偷懒。"),
    },
    "钱财": {
        "positive": ("忌：赚到一点就开始飘。", "忌：看见利好就忘了风险。"),
        "neutral": ("忌：被折扣和活动文案催眠。", "忌：一边纠结一边下单。"),
        "caution": ("忌：听不懂还硬上。", "忌：为了回本心态做加码。"),
    },
    "选择": {
        "positive": ("忌：选了还一直回头想另一条路。", "忌：把“想试试”拖成又一个悬案。"),
        "neutral": ("忌：因为怕错过而什么都舍不得放。", "忌：把短期情绪当长期答案。"),
        "caution": ("忌：为了赶进度随便选。", "忌：只看诱惑不看代价。"),
    },
    "通用": {
        "positive": ("忌：顺风局里过度自信。", "忌：想太久却迟迟不动。"),
        "neutral": ("忌：还没开始就预设结局。", "忌：把正常波动误判成失败。"),
        "caution": ("忌：硬扛和死撑。", "忌：在糟糕状态下做重要决定。"),
    },
}
_DRAW_SUMMARIES = {
    2: ("一句话：可以上，但别飘，稳稳推进最容易赢。", "一句话：今天的运更像在给你开路，记得自己迈步。"),
    1: ("一句话：偏吉，值得试，但靠节奏比靠蛮劲更有用。", "一句话：你可以往前走，关键是别自己先乱了阵脚。"),
    0: ("一句话：这题不靠赌，靠稳。", "一句话：先把小步走实，答案会在路上变清楚。"),
    -1: ("一句话：不是不行，是先别急。", "一句话：先收住风险，再谈下一步，会更像赢法。"),
    -2: ("一句话：今天最强的操作，叫暂缓和止损。", "一句话：先保状态，再谈结果，这不丢人。"),
}


def _safe_get_message_text(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_message_str() or "").strip()
    except Exception:
        return ""


def _safe_get_sender_id(event: AstrMessageEvent) -> str:
    try:
        value = event.get_sender_id()
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_get_sender_name(event: AstrMessageEvent) -> str:
    try:
        value = event.get_sender_name()
        if value:
            return str(value).strip()
    except Exception:
        pass
    return "你"


def _extract_command_body(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if text[:1] in {"/", "!", "！"}:
        text = text[1:].strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _build_rng(*parts: Any) -> random.Random:
    raw = "||".join(str(part or "").strip() for part in parts)
    digest = sha256(raw.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _pick_one(rng: random.Random, values: Sequence[str]) -> str:
    if not values:
        return ""
    return str(values[rng.randrange(len(values))]).strip()


def _pick_many_unique(rng: random.Random, values: Sequence[str], count: int) -> list[str]:
    items = [str(item).strip() for item in values if str(item).strip()]
    if not items or count <= 0:
        return []
    indexed = list(enumerate(items))
    indexed.sort(key=lambda item: rng.random())
    selected: list[str] = []
    seen: set[str] = set()
    for _index, value in indexed:
        if value in seen:
            continue
        selected.append(value)
        seen.add(value)
        if len(selected) >= count:
            break
    return selected


def _default_meal_by_time(now: datetime) -> str:
    hour = now.hour
    if hour < 10:
        return "早餐"
    if hour < 14:
        return "午饭"
    if hour < 17:
        return "下午茶"
    if hour < 22:
        return "晚饭"
    return "夜宵"


def _detect_meal_label(text: str) -> str:
    content = str(text or "").strip()
    for label, keywords in _MEAL_KEYWORDS:
        if any(keyword in content for keyword in keywords):
            return label
    return _default_meal_by_time(datetime.now())


def _detect_style_labels(text: str) -> list[str]:
    content = str(text or "").strip()
    labels: list[str] = []
    for label, keywords in _STYLE_KEYWORDS:
        if any(keyword in content for keyword in keywords):
            labels.append(label)
    return labels


def _build_persona_text(event: AstrMessageEvent, body: str) -> str:
    sender_name = _safe_get_sender_name(event)
    sender_key = _safe_get_sender_id(event) or sender_name
    prompt_tag = body or "默认"
    rng = _build_rng("daily_persona", _today_key(), sender_key, prompt_tag)
    persona_name = f"{_pick_one(rng, _PERSONA_PREFIXES)}{_pick_one(rng, _PERSONA_ROLES)}"
    lines = [
        f"🎭 {sender_name} 的今日人设",
        f"身份：{persona_name}",
        f"气场：{_pick_one(rng, _PERSONA_AURAS)}",
        _pick_one(rng, _PERSONA_BUFFS),
        _pick_one(rng, _PERSONA_NERFS),
        _pick_one(rng, _PERSONA_QUESTS),
        _pick_one(rng, _PERSONA_LINES),
        _pick_one(rng, _PERSONA_SIDEKICKS),
    ]
    if body:
        lines.append(f"关键词滤镜：{body}")
    lines.append("提示：结果按“日期 + 你 + 关键词”生成，今天重复抽取会保持一致。")
    return "\n".join(lines)


def _build_food_text(event: AstrMessageEvent, raw_text: str, body: str) -> str:
    sender_key = _safe_get_sender_id(event) or _safe_get_sender_name(event)
    request_text = f"{raw_text}\n{body}".strip()
    meal = _detect_meal_label(request_text)
    styles = _detect_style_labels(request_text)
    rng = _build_rng("eat_what", _today_key(), sender_key, meal, "|".join(styles), body or "默认")

    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for item in _FOOD_ITEMS:
        meals = item.get("meals") or set()
        if meal not in meals:
            continue
        tags = set(item.get("tags") or set())
        score = 0
        for style in styles:
            if style in tags:
                score += 3
        if not styles and "经典" in tags:
            score += 1
        candidates.append((score, rng.random(), item))

    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    ranked_names = [str(item.get("name") or "").strip() for _score, _tie, item in candidates]
    suggestions = _pick_many_unique(rng, ranked_names, 3)
    if len(suggestions) < 3:
        fallback_names = [
            str(item.get("name") or "").strip()
            for item in _FOOD_ITEMS
            if meal in (item.get("meals") or set())
        ]
        for name in _pick_many_unique(rng, fallback_names, 5):
            if name not in suggestions:
                suggestions.append(name)
            if len(suggestions) >= 3:
                break

    drink_pool = list(_MEAL_DRINKS.get(meal, ()))
    for style in styles:
        drink_pool.extend(_STYLE_DRINKS.get(style, ()))
    drink = _pick_one(rng, drink_pool or ("白开水",))

    tip_pool = list(_MEAL_TIPS.get(meal, ()))
    for style in styles:
        tip_pool.extend(_STYLE_TIPS.get(style, ()))
    tip = _pick_one(rng, tip_pool or ("今天先吃到嘴比什么都重要。",))
    lazy_plan = _pick_one(rng, _LAZY_PLANS.get(meal, ("偷懒方案：选最稳定那家。",)))
    final_choice = _pick_one(rng, suggestions or ("今天先吃点热乎的。",))

    scene_text = f"{meal} · {' / '.join(styles)}" if styles else meal
    lines = [
        "🍽 吃饭决策器",
        f"当前场景：{scene_text}",
        "今日建议：",
    ]
    for index, suggestion in enumerate(suggestions, start=1):
        lines.append(f"{index}. {suggestion}")
    lines.extend(
        (
            f"饮品签：{drink}",
            f"纠结到最后就选：{final_choice}",
            lazy_plan,
            f"小提醒：{tip.strip()}",
            "提示：结果按“日期 + 你 + 场景关键词”生成，想换风格可补充如“减脂/重口/夜宵/一个人”。",
        )
    )
    return "\n".join(lines)


def _detect_draw_domain(question: str) -> str:
    content = str(question or "").strip()
    for label, keywords in _DRAW_DOMAIN_KEYWORDS:
        if any(keyword in content for keyword in keywords):
            return label
    return "通用"


def _score_bucket(score: int) -> str:
    if score >= 1:
        return "positive"
    if score <= -1:
        return "caution"
    return "neutral"


def _build_draw_text(event: AstrMessageEvent, question: str) -> str:
    sender_key = _safe_get_sender_id(event) or _safe_get_sender_name(event)
    normalized_question = question or "我今天整体运势如何"
    domain = _detect_draw_domain(normalized_question)
    rng = _build_rng("fortune_draw", _today_key(), sender_key, normalized_question)
    sign = _SIGNS[rng.randrange(len(_SIGNS))]
    score = int(sign.get("score") or 0)
    bucket = _score_bucket(score)
    opener = _pick_one(rng, _DRAW_OPENERS.get(domain, _DRAW_OPENERS["通用"]))
    analysis = _pick_one(rng, _DRAW_ANALYSIS.get(bucket, _DRAW_ANALYSIS["neutral"]))
    action = _pick_one(rng, _DRAW_ACTIONS.get(domain, _DRAW_ACTIONS["通用"]).get(bucket, ()))
    taboo = _pick_one(rng, _DRAW_TABOOS.get(domain, _DRAW_TABOOS["通用"]).get(bucket, ()))
    summary = _pick_one(rng, _DRAW_SUMMARIES.get(score, _DRAW_SUMMARIES[0]))

    return "\n".join(
        (
            "🔮 今日一签",
            f"所问：{normalized_question}",
            f"签面：{sign.get('level')} · {sign.get('title')}",
            f"签语：{sign.get('line')}",
            f"解签：{opener}{analysis}",
            f"宜：{action}",
            taboo,
            summary,
            "提示：结果按“日期 + 你 + 问题”生成，同一天问同一个问题会保持一致。",
        )
    )


def _persona_help_text() -> str:
    return (
        "用法：`/今日人设 [关键词]`\n"
        "示例：\n"
        "- `/今日人设`\n"
        "- `/今日人设 打工版`\n"
        "- `/今日人设 社交局`\n"
        "说明：会按当天日期、你的身份和可选关键词生成一个限定人设。"
    )


def _food_help_text() -> str:
    return (
        "用法：`/吃什么 [场景关键词]`\n"
        "示例：\n"
        "- `/吃什么`\n"
        "- `/今天吃什么 晚饭 减脂`\n"
        "- `/吃什么 夜宵 一个人`\n"
        "可识别关键词示例：早餐、午饭、晚饭、夜宵、下午茶、减脂、清淡、重口、省钱、一个人、聚餐、尝鲜。"
    )


def _draw_help_text() -> str:
    return (
        "用法：`/抽签 [问题]`\n"
        "示例：\n"
        "- `/抽签`\n"
        "- `/抽签 这周要不要推进副业`\n"
        "- `/求签 我该不该现在表白`\n"
        "说明：不写问题会抽一支通用签；写得越具体，解签越贴近当下场景。"
    )


async def handle_daily_persona_command(event: AstrMessageEvent):
    raw_text = _safe_get_message_text(event)
    body = _extract_command_body(raw_text)
    if body.lower() in _HELP_TOKENS or body in _HELP_TOKENS:
        yield event.plain_result(_persona_help_text())
        return
    yield event.plain_result(_build_persona_text(event, body))


async def handle_eat_what_command(event: AstrMessageEvent):
    raw_text = _safe_get_message_text(event)
    body = _extract_command_body(raw_text)
    if body.lower() in _HELP_TOKENS or body in _HELP_TOKENS:
        yield event.plain_result(_food_help_text())
        return
    yield event.plain_result(_build_food_text(event, raw_text, body))


async def handle_draw_lot_command(event: AstrMessageEvent):
    raw_text = _safe_get_message_text(event)
    body = _extract_command_body(raw_text)
    if body.lower() in _HELP_TOKENS or body in _HELP_TOKENS:
        yield event.plain_result(_draw_help_text())
        return
    yield event.plain_result(_build_draw_text(event, body))
