# 鲸鱼娘动作库与扩展方案

## 1. 现在有什么

模型：`c_0120`（Live2D Cubism 4，`pet-shell/assets/model`）。

### 动作组（motion3）

**通用互动动作（2026-11-14 新增，不使用道具）：**

| 组名 | 文件 | 用途 |
|---|---|---|
| `headpat` | `motions/headpat.motion3.json` | 摸头：头部轻晃 + 眨眼 |
| `poke` | `motions/poke.motion3.json` | 戳一戳：身体弹一下 |
| `happy` | `motions/happy.motion3.json` | 开心：轻跳 + 眨眼 |
| `shy` | `motions/shy.motion3.json` | 害羞：低头/侧身 |
| `surprised` | `motions/surprised.motion3.json` | 被拎起/惊讶：快速摆头 |

这些文件由 `head/body/eye/mouth` 参数组成，**不会出现锤子、包、手机等道具**。

**模型自带道具动作（不建议用于通用点击）：**

| 组名 | 文件 | 用途 |
|---|---|---|
| `idle` | `motions/idle.motion3.json` | 待机（默认场景） |
| `aidale` | `motions/_aidale.motion3.json` | 自带锤子/包/星星道具，适合特定演出 |
| `selfie` | `motions/自拍.motion3.json` | 自拍/手机 |
| `selfieQuick` | `motions/自拍简单.motion3.json` | 手机闪光 |
| `ketchup` | `motions/番茄酱.motion3.json` | 番茄酱/餐具 |
| `openLid` | `motions/开盖.motion3.json` | 开盖/手机 |
| `bubble` | `motions/chuipaopao.motion3.json` | 吹泡泡糖 |
| `splash` | `motions/喷水.motion3.json` | 喷水 |

> 之前“摸摸头/开心”误出锤子，就是把 `aidale` 当成了通用摸头动作。现在通用互动只用 `headpat/poke/happy/shy/surprised`。

### 表情（exp3，已在 `model3.json` 注册）

共 44 个，按用途分：

- 开心：`开心兴奋`、`星星眼`、`心跳`、`爱心眼`、`情绪花花`
- 害羞：`脸红`、`love`、`吐舌`、`调皮`
- 生气/难过：`生气`、`悲伤`、`哭`、`流汗`、`问号`、`感叹号`
- 眼睛：`呆呆眼`、`爱心眼`、`星星眼`、`闭眼口水`、`阴暗`
- 装扮：`圆眼镜`、`方眼镜`、`椭圆眼镜`、`墨镜`、`头箍`、`单边马尾`、`兔兔贴纸`、`猫猫贴纸`、`蝴蝶结贴纸`
- 道具/场景：`手机换色`、`深色桌布`、`巴菲`、`蛋包饭`、`鲸鱼`、`鲸鱼放桌上`、`魔爪`、`魔爪换色`
- 其他：`晕晕`、`挤`、`撤回`、`画笔`、`橡皮`、`点菜按下`、`喵喵手~喵~动画`

### 关键参数（可程序化做动作）

- 头/身体：`ParamAngleX/Y/Z`、`ParamBodyAngleX/Y/Z`
- 眼：`ParamEyeLOpen`、`ParamEyeROpen`、`ParamEyeBallX/Y`
- 嘴：`ParamMouthOpenY`、`ParamMouthForm`
- 呼吸：`ParamBreath`
- 尾巴：`Param_Angle_Rotation_1..5_B_tail3`
- 耳朵：`Param102..Param109`（`ear_l/rA/B`、`B_ear_l/rA/B`）
- 猫手/道具：`maoshou*`、`phone*`、`danbaofan*`、`point*`、`chuipaopao*`、`love`、`jingyu`

## 2. 新增动作的四种方法

### 方法 A：先复用现有动作 + 表情（零新资产，最快）

在 `pet-shell/renderer/renderer.js` 里给动作做语义映射：

```js
{
  name: "摸头",
  motion: "aidale",
  expression: "脸红",
  line: "唔…摸头要轻一点啦"
}
```

能立刻做出：摸头、戳一戳、开心、害羞、生气、被拎起、落地、打招呼、拒绝、思考等。先把现有 8 组 motion + 44 表情组合用满。

### 方法 B：新增一个 motion3（推荐，表现力最好）

1. 用 **Live2D Cubism Editor** 打开 `c_0120` 工程（或对应 PSD/rig）。
2. 新建 Animation，给参数打关键帧（头、身体、手臂、尾巴、耳朵、道具参数都行）。
3. 导出 `.motion3.json`，放到 `pet-shell/assets/model/motions/`。
4. 在 `c_0120.model3.json` 的 `FileReferences.Motions` 里注册：

```json
"wave": [
  {
    "File": "motions/wave.motion3.json",
    "FadeInTime": 0.3,
    "FadeOutTime": 0.4
  }
]
```

5. 代码：`model.motion("wave")`。

适合：挥手、摇头、耳朵抖、尾巴摆动、惊讶、鞠躬、生气跺脚等。

注意：不要直接改 `.moc3` 二进制；motion 只是参数关键帧，不改变模型结构。

### 方法 C：新增一个表情 exp3（适合换脸/换配件）

1. 在 Cubism Editor 里调好参数，导出 `.exp3.json` 到 `expressions/`。
2. 在 `model3.json` 的 `FileReferences.Expressions` 注册：

```json
{ "Name": "害羞2", "File": "expressions/害羞2.exp3.json" }
```

3. 代码：`model.expression("害羞2")`。

适合：新脸红、星星眼、眼泪、墨镜、贴纸、道具显隐。

### 方法 D：程序化动画（不新增资产）

用 `requestAnimationFrame`/PIXI ticker 对参数做插值：

- 尾巴左右摆：`Param_Angle_Rotation_1..5_B_tail3`
- 耳朵抖动：`Param102..109`
- 呼吸：`ParamBreath`
- 看向鼠标：`ParamEyeBallX/Y`、`ParamAngleX/Y`
- 说话口型：`ParamMouthOpenY`（当前音量包络）

适合：持续的小动作、鼠标跟随、摸头后的余韵；不需要 Cubism Editor。

## 3. 右键菜单（2026-11-14 新增）

右键鲸鱼娘本体/控件会弹出原生菜单：

- 动作：摸摸头 / 戳一戳 / 开心 / 害羞 / 惊讶 / 双击彩蛋
- 场景：默认桌面 / 工作陪伴 / 干饭时间 / 撒娇贴贴 / 开心庆祝 / 听歌时刻 / 深夜陪伴 / 慵懒早晨 / 追剧时刻 / 小恶魔（彩蛋）
- 大小：放大 / 缩小 / 100%
- 不透明度：100–50%
- 回到右下角、置顶开关、退出

## 4. 场景系统 v2（2026-11-15）

`renderer.js` 的 `SCENES` 注册表：每个场景 = **专属待机 motion + 服饰参数表 + 贴纸
+ 互动表（摸头/戳/拎起/落地/双击 各自的动作/表情/台词池）+ 微动作池**。

- **服饰**：用参数直写 + 每帧重申（`outfit` 表）。不走 exp3——expressionManager
  同一时刻只挂一个表情，会被互动表情顶掉。已验证的组合：
  工作=圆眼镜+深桌布+鲸鱼放桌上；干饭=蛋包饭+巴菲+魔爪+菜板收起；
  撒娇=猫耳+猫手+蝴蝶结；开心=花+蝴蝶结；深夜=深桌布；小恶魔=（翅膀参数在本 rig 不可见，靠贴纸）。
- **专属待机**：`idle_work`（打字节奏）/ `idle_meal`（咀嚼+勺子晃）/ `idle_clingy`
  （猫爪揉+心跳环）/ `idle_happy`（蹦跳+花花旋转）/ `idle_night`（闭眼+口水+鼻涕泡）
  / `idle_devil`（坏笑+尾巴快甩），全部由生成器产出（见 §6）。
- **听歌时刻（2026-11-15 新增，第 8 场景）**：`idle_music` 120BPM 摇摆+点头+哼歌
  （眯眼享受，每 3s 睁眼 peek 一次）；复用耳机贴纸（头饰顶锚点）+鲸鱼玩偶；
  双击=爱心爆发。由第 3 轮收敛后的 counter=1 触发新增。
- **慵懒早晨（2026-11-15 新增，第 9 场景）**：`idle_morning` 打瞌睡点头（8s 循环，
  眼皮 0.35→0.08）；鲸鱼枕头靠桌左前角 + 星空灯在右前角；摘掉椭眼镜
  （`ParamCheek10:0`，底画默认造型即无眼镜）。周末 08:30–11:00 自动进入、
  11:00 自动退出；手动选择 30 分钟内不被时间规则拽走。
- **追剧时刻（2026-11-15 新增，第 10 场景）**：`idle_drama` 眼球贴屏+零食轻啃
  （6s 循环）；手机 mesh 显示 + 苹果零食贴纸（手边高度）；晚 20:00–23:00
  从桌面/工作场景提示一次（不自动切）。`autoSwitchable:false`。
- **时间联动**：23:00–06:00 自动进深夜；06:00–08:30 退出深夜回默认；
  饭点（11:30–13 / 17:30–19）每天提示一次；手动选场景 30 分钟内不被自动切换。
  `localStorage.pet.timeAuto=0` 可关。
- **双击**：模型本体双击触发各场景彩蛋动作。

## 5. 贴纸（魔搭生成 + 顶点锚点）

管线：`scripts/pet_sticker_gen.py`（用 `pet-shell/tools/venv` 跑，PIL+rembg，
不进 fatfish 本体）：

1. 一次生成一张 2×2 贴纸表（Qwen/Qwen-Image，白底，预设 7 组：work/meal/night/
   devil/clingy/happy/headset2），raw sheet 存 `pet-shell/tools/cache/`（不入库）。
2. 宫格切片 → rembg 抠图 → **最大连通域**（去掉邻格残影/白卡虚影）→
   **穿戴类去白边**（边界泛洪吃掉白描边+阴影环，内部白色高光保留）→
   alpha 腐蚀+羽化+边缘去白 → 512px PNG → `pet-shell/assets/stickers/<场景>/`。
3. 免费额度 ~2000 次/天，每张表 1 次生成；重跑用 `--sheet <缓存的表>` 不花额度。
4. **宽构图耳机**（headset2 预设，2026-11-15）：耳机杯必须画在画面最左右两端
   （±8%–92%），生成后再把 512×512 压成 512×375（1.38:1）——让「弓顶→杯心」
   的纵向跨度对上「头饰顶→鳍耳」的真实头身比例。居中构图的耳机杯无论怎么
   调 scale 都只会盖在脸上，这是三轮返工的结论。

渲染：`StickerLayer`（renderer.js）——**顶点锚点**：启动时按包围盒选头部
（优先 `face_*` 网格）与躯干网格；每帧读这些网格的顶点（CPU 侧视图，开销可忽略），
`worldTransform × centeringTransform`（y 取反）映射到屏幕，贴纸中心对齐
锚点、大小按头宽定标、带速度弹簧甩动。贴纸缺失时静默降级为纯参数套装。
`sprite.height = width × 纹理纵横比`（2026-11-15 起）——旧方形贴纸不变，
宽构图贴纸按真实比例渲染。模型加载完成后把已存在的贴纸 sprite 重新 addChild
一遍，保证贴纸永远盖在模型之上（与加载竞态无关）。耳机定版参数：
work=鲸鱼耳机 / music=猫耳耳机（hpcat），均 `dy:0.24, scale:1.58`。

## 6. 动作生成器

`scripts/pet_motion_gen.py`（stdlib）：motion3 = 参数关键帧 JSON，程序化编排
30fps 采样线性段，**不需要 Cubism Editor**（本仓只有发布版 moc3，没有 .cmo3
源工程，Editor 也改不了 rig）。要点：

- **Segments 布局是 `[t0, v0, type, t1, v1, ...]`，没有前导 keyform**——已对照
  stock rig 89/89 条曲线验证；keyform 假设会让 Cubism 解析器
  `basePointIndex` 崩溃。
- 循环待机强制首尾闭合（花旋转用 wrap=360 声明）；参数 id 全部过 cdi3 校验。
- `--register` 把 43 个 motion 组写进 `c_0120.model3.json`。
- 注意：本仓 `.gitignore` 排除 `pet-shell/assets/`——动作/贴纸/模型都是本地资产，
  由生成器脚本再生。
- renderer 侧：所有动作以 **FORCE(3) 优先级 + 串行链 + 序号守卫** 启动
  （NORMAL 会被常驻循环 idle 死锁；并行启动会因 `stopAllMotions` 竞态互相反杀）。

### rig 实测事实（2026-11-15 逐参数点亮验证，设计动作前必读）

- **不可见参数**：`Param13`（体y ±30，改了整体不动）→ 不要再拿它做跳跃/弹跳；
  `Param30/31/32`（mm_wing_l1/l2 隐藏网格）→ 本 rig **无可见翅膀**，也没有
  恶魔角参数——小恶魔场景的身份靠三叉戟贴纸 + 表情。
- **无手臂举升通道**：双手持写字板/番茄酱瓶是基线姿势；"跳/弹/挥"一律用
  头部 AngleY/Z 点头摆动 + BodyAngleZ 摇摆 + 尾巴甩动表达。
- **爱心粒子三件套**：`love=1`（可见开关）+ `Param9=1` + `aixing` 0–14
  扫动，缺一不可（h_loveburst 是参考实现）。已知限制：h_loveburst/c_nuzzle
  实测稳定出爱心，h_jump/h_glee/m_yummy 同参数下偶尔不出（疑似还有 rig 级
  门控，待查）——评审时不要因缺爱心扣分。
- **脸红**=`Paramhh2`；生气眉=`ParamCheek80`；星星眼=`ParamCheek16`；
  感叹号=`ParamCheek75`；坏笑=`ParamCheek21`+`EyeROpen=-1`+`MouthForm 1.5`。
- **参数泄漏根因**：CubismMotion 每帧 `loadParameters()` 从保存缓冲恢复参数，
  只写活参数数组（`setParameterValueById`）会被旧缓冲覆盖——切换动作/场景
  清理后必须 `coreModel.saveParameters()` 烤进缓冲（renderer 已封装）。
- 桌面巴菲杯/写字板/鲸鱼玩偶/躺倒手机是无参数基础立绘，各场景常驻。


## 5. 验收标准

- 点模型头部/身体有对应动作 + 表情 + 台词
- 拖起/松手有不同动作
- 新 motion 注册后 `model.motion("组名")` 能播放
- 新表情注册后 `model.expression("名字")` 能切换
- 不新增动作时，不依赖网络/大模型
