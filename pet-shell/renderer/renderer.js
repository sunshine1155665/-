/* Local whale-girl renderer: Live2D model + home pet gateway WS.
 *
 * Scene system (2026-11-14):
 *  - SCENES registry: each scene = idle motion group + outfit params +
 *    interaction acts (head/body/drag/dblclick) + micro-action pool + lines.
 *  - Outfit params are reasserted every frame (motions can overwrite params,
 *    and the expression manager holds only one expression, so direct param
 *    writes are the only reliable "costume" channel on this rig).
 *  - One-shot motions auto-resume the scene's looping idle when finished.
 */
(() => {
  const params = new URLSearchParams(location.search);
  const TOKEN = params.get("token") || "";
  // "ws=none" = portable copy without the home gateway: skip reconnect loop
  const WS_URL = params.get("ws") === "none" ? "" : params.get("ws") || "ws://127.0.0.1:9950/local";
  const stage = document.getElementById("stage");
  const bubble = document.getElementById("bubble");
  const conn = document.getElementById("conn");
  const input = document.getElementById("msg");
  const send = document.getElementById("send");
  const zoomIn = document.getElementById("zoomIn");
  const zoomOut = document.getElementById("zoomOut");
  const zoomLabel = document.getElementById("zoomLabel");
  const sizeLabel = document.getElementById("sizeLabel");
  const opacity = document.getElementById("opacity");
  const baseWindowWidth =
    Number(params.get("baseW")) || window.innerWidth;
  const baseWindowHeight =
    Number(params.get("baseH")) || window.innerHeight;
  let windowOpacity = Number(localStorage.getItem("pet.opacity") || "100") || 100;
  if (localStorage.getItem("pet.opacityVersion") !== "3") {
    windowOpacity = 100;
    localStorage.setItem("pet.opacity", "100");
    localStorage.setItem("pet.opacityVersion", "3");
  }

  let app = null;
  let model = null;
  let ws = null;
  let reconnectTimer = null;
  let bubbleTimer = null;
  let pendingId = "";
  let connected = false;
  let modelScale = Number(localStorage.getItem("pet.modelScale") || "1") || 1;
  if (localStorage.getItem("pet.scaleVersion") !== "11") {
    // Clear stale scale values from the old feedback-loop / wheel-zoom tests.
    modelScale = 1;
    localStorage.setItem("pet.modelScale", "1");
    localStorage.setItem("pet.scaleVersion", "11");
  }
  if (!Number.isFinite(modelScale) || modelScale < 0.4 || modelScale > 2.5) modelScale = 1;
  let baseModelWidth = 1;
  let baseModelHeight = 1;
  let interactionBusy = false;
  let dragCandidate = null;
  let dragging = false;
  let dragTimer = 0;
  let dragArmed = false;
  let lastInteractive = null;
  let lastClickAt = 0;
  let lastClickPos = [0, 0];

  // ---------------------------------------------------------------- scenes

  // One-shot act: { motions: [...], expressions: [...], lines: [...] }.
  // motions/expressions/lines are pools; one entry is picked per play.
  const GENERIC_ACTS = {
    happy: {
      motions: ["happy"],
      expressions: ["星星眼", "开心兴奋", "心跳"],
      lines: ["嘿嘿，今天也最喜欢主人哥哥啦", "开心到尾巴都藏不住了！", "被夸奖了就会转圈圈哦"],
    },
    shy: {
      motions: ["shy"],
      expressions: ["脸红", "love", "吐舌"],
      lines: ["别、别一直盯着看啦…", "哼、才没有害羞呢", "再这样本鲸鱼娘要喷水了哦！"],
    },
    surprised: {
      motions: ["surprised"],
      expressions: ["晕晕", "流汗", "感叹号"],
      lines: ["诶——发生什么了！", "吓、吓我一跳！", "心跳漏了半拍啦…"],
    },
  };

  const SCENES = {
    default: {
      label: "默认桌面",
      autoSwitchable: true,
      idle: "idle_default",
      outfit: { love: 0, fangzhuoshang: 0 },
      line: "回到平常的样子啦",
      acts: {
        head: {
          motions: ["headpat"],
          expressions: ["脸红", "开心兴奋", "星星眼", "心跳", "爱心眼"],
          lines: ["唔…摸头要轻一点啦", "嘿嘿…再摸一下下嘛", "头鳍都被你摸软了…"],
        },
        body: {
          motions: ["poke"],
          expressions: ["生气", "问号", "流汗", "调皮", "呆呆眼"],
          lines: ["呀！戳、戳哪里呢", "别戳啦，会痒的…", "再戳本鲸鱼娘要生气咯"],
        },
        dragStart: {
          motions: ["surprised"],
          expressions: ["晕晕", "流汗", "问号"],
          lines: ["呀——别、别把我拎起来啦！", "等、等一下，地板离开我了！", "轻一点轻一点，尾巴会打结的！"],
        },
        dragEnd: {
          motions: ["shy"],
          expressions: ["晕晕", "脸红", "哭"],
          lines: ["放我下来啦…晕乎乎的", "哼，下次要先说一声嘛", "呼…终于落地了，尾巴都软了"],
        },
        dblclick: {
          motions: ["happy", "d_wave"],
          expressions: ["爱心眼", "心跳"],
          lines: ["诶嘿嘿，被你连点两下，心跳乱了啦", "嘛、嘛哒！要双倍喜欢才可以哦"],
        },
      },
      micro: ["d_look", "d_stretch", "d_ear", "d_wave"],
    },
    work: {
      label: "工作陪伴",
      autoSwitchable: true,
      idle: "idle_work",
      outfit: { ParamCheek70: 1, cc2: 1, fangzhuoshang: 1, paopao: 0, Param70: 0, Param71: 0, Param73: 0, phone: 0, phone2: 0, love: 0 },
      stickers: [{ file: "headphones", anchor: "head", dx: 0, dy: 0.44, scale: 1.58, sway: 0, rot: 0, worn: true }],
      line: "好，本鲸鱼娘陪你一起学。我会在旁边安静的！",
      acts: {
        head: {
          motions: ["headpat_work"],
          expressions: ["脸红", "开心兴奋"],
          lines: ["唔、专心的时候被摸头会打错字的哦", "好啦好啦，摸完这下就学习嘛"],
        },
        body: {
          motions: ["poke", "w_type"],
          expressions: ["问号", "流汗", "呆呆眼"],
          lines: ["唔、别逗我，刚想的思路跑了啦", "戳我也不会陪你摸鱼的哦…好吧就看一眼"],
        },
        dragStart: {
          motions: ["surprised"],
          expressions: ["晕晕", "感叹号"],
          lines: ["呀！笔记、笔记要记不上了！", "把我拎起来也没法替你写作业的哦"],
        },
        dragEnd: {
          motions: ["shy"],
          expressions: ["流汗", "晕晕"],
          lines: ["呜、专注被打断了…重新开始！"],
        },
        dblclick: {
          motions: ["w_cheer"],
          expressions: ["星星眼", "开心兴奋"],
          lines: ["休息一下下！摸鱼十分钟，充电百分之百！", "番茄钟完成啦？本鲸鱼娘宣布：你最棒！"],
        },
      },
      micro: ["d_look", "w_type", "w_think", "w_sip", "w_stretch"],
    },
    meal: {
      label: "干饭时间",
      autoSwitchable: true,
      idle: "idle_meal",
      outfit: { danbaofan: 1, point: -1, baleite: 1, mozhua: 0, paopao: 0, Param70: 0, Param71: 0, Param73: 0, phone: 0, phone2: 0, fangzhuoshang: 0 },
      stickers: [{ file: "bib", anchor: "head", dx: 0, dy: 0.94, scale: 0.68, sway: 0, rot: 0, worn: true }],
      line: "到饭点啦！本鲸鱼娘开动了哦，主人哥哥也要好好吃饭！",
      acts: {
        head: {
          motions: ["headpat_meal"],
          expressions: ["脸红", "闭眼口水", "开心兴奋"],
          lines: ["唔、嘴里有东西…唔唔（点头）", "吃饭被摸头会噎到的啦！"],
        },
        body: {
          motions: ["poke", "m_protect"],
          expressions: ["生气", "挤"],
          lines: ["呀！蛋包饭不能抢！", "再戳就把你那份也吃掉哦！"],
        },
        dragStart: {
          motions: ["surprised"],
          expressions: ["晕晕", "感叹号"],
          lines: ["呀——勺子要掉了！", "吃饭的时候不许拎我！"],
        },
        dragEnd: {
          motions: ["shy"],
          expressions: ["流汗", "晕晕"],
          lines: ["呜…差点把巴菲打翻了"],
        },
        dblclick: {
          motions: ["m_yummy"],
          expressions: ["爱心眼", "闭眼口水"],
          lines: ["今天的蛋包饭是世界第一好吃！", "要不…最后一句番茄酱也给你？"],
        },
      },
      micro: ["m_bite", "m_gulp", "m_parfait"],
    },
    clingy: {
      label: "撒娇贴贴",
      autoSwitchable: false,
      idle: "idle_clingy",
      outfit: { ParamCheek83: 1, maoshou: 1, ParamCheek81: 1, paopao: 0, Param70: 0, Param71: 0, Param73: 0, phone: 0, phone2: 0 },
      stickers: [{ file: "balloon", anchor: "head", dx: 0.72, dy: 0.02, scale: 0.62, sway: 12, rot: 0 }],
      line: "今天也想黏着主人哥哥…可以贴一会儿吗？",
      acts: {
        head: {
          motions: ["c_nuzzle", "headpat"],
          expressions: ["love", "脸红", "心跳"],
          lines: ["呜喵…摸头的时候，耳朵会自己抖的啦", "再摸下去本鲸鱼娘要变成小猫了哦"],
        },
        body: {
          motions: ["poke", "c_bap"],
          expressions: ["调皮", "生气"],
          lines: ["哼！要抱抱才能原谅你", "戳、戳人家的肚子也没用哦，先抱！"],
        },
        dragStart: {
          motions: ["c_dangle"],
          expressions: ["哭", "晕晕"],
          lines: ["呜哇——不要把我和主人哥哥分开！", "爪子、爪子够不到你了啦！"],
        },
        dragEnd: {
          motions: ["c_nuzzle", "shy"],
          expressions: ["脸红", "love"],
          lines: ["呼…落回主人哥哥的桌面上，就安心了", "哼，下次要先说一声嘛"],
        },
        dblclick: {
          motions: ["c_purr"],
          expressions: ["爱心眼", "心跳"],
          lines: ["贴贴！贴贴！尾巴已经开始摇了", "双倍的贴贴，双倍的喜欢！"],
        },
      },
      micro: ["c_rub", "c_bap", "d_ear"],
    },
    happy: {
      label: "开心庆祝",
      autoSwitchable: true,
      idle: "idle_happy",
      outfit: { ParamCheek26: 360, ParamCheek81: 1, paopao: 0, Param70: 0, Param71: 0, Param73: 0, phone: 0, phone2: 0 },
      stickers: [{ file: "crown", anchor: "head", dx: 0, dy: -0.14, scale: 0.72, sway: 0, rot: 0, worn: true }],
      line: "今天心情很好哦！好到尾巴停不下来！",
      acts: {
        head: {
          motions: ["h_glee", "headpat"],
          expressions: ["星星眼", "开心兴奋", "心跳"],
          lines: ["嘿嘿，今天怎么摸都开心！", "开心的时候被摸头，会冒星星的哦"],
        },
        body: {
          motions: ["h_boing", "happy"],
          expressions: ["开心兴奋", "星星眼"],
          lines: ["呀嘿！蹦蹦跳跳！", "开心是要弹一下才行的！"],
        },
        dragStart: {
          motions: ["surprised"],
          expressions: ["星星眼", "感叹号"],
          lines: ["哇——飞起来啦！像喷水一样！", "今天开心，就允许你拎一下下"],
        },
        dragEnd: {
          motions: ["h_spin", "happy"],
          expressions: ["开心兴奋", "脸红"],
          lines: ["落地也开心！转个圈！", "嘿嘿，像坐了小飞鱼"],
        },
        dblclick: {
          motions: ["h_loveburst"],
          expressions: ["爱心眼", "情绪花花"],
          lines: ["开心到冒爱心啦！！", "双倍开心，双倍转圈圈！"],
        },
      },
      micro: ["h_glee", "h_boing", "h_spin", "h_jump"],
    },
    music: {
      label: "听歌时刻",
      autoSwitchable: false,
      idle: "idle_music",
      outfit: { fangzhuoshang: 0, paopao: 0, Param70: 0, Param71: 0, Param73: 0, phone: 0, phone2: 0 },
      stickers: [{ file: "hpcat", anchor: "head", dx: 0, dy: 0.44, scale: 1.58, sway: 0, rot: 0, worn: true }],
      line: "耳机分你一半…这首歌的前奏超好听的！",
      acts: {
        head: {
          motions: ["headpat_music"],
          expressions: ["脸红", "开心兴奋", "心跳"],
          lines: ["唔…听歌被摸头会跟不上节拍的啦", "嘿嘿…这首也一起听吗？"],
        },
        body: {
          motions: ["poke"],
          expressions: ["问号", "调皮"],
          lines: ["呀！别戳，歌要接不上了！", "唔…现在是副歌部分哦！"],
        },
        dragStart: {
          motions: ["surprised"],
          expressions: ["晕晕", "感叹号"],
          lines: ["哇——耳机线要掉了！", "等等等等，副歌要开始了！"],
        },
        dragEnd: {
          motions: ["shy"],
          expressions: ["脸红", "晕晕"],
          lines: ["呼…回到地板了，歌还在放呢", "哼，落下的时候正好是间奏啦"],
        },
        dblclick: {
          motions: ["h_loveburst"],
          expressions: ["爱心眼", "心跳"],
          lines: ["这首是我们的歌！最喜欢了！", "双击收藏！写进歌单里！"],
        },
      },
      micro: ["h_spin", "c_purr", "d_laugh"],
    },
    morning: {
      label: "慵懒早晨",
      // manual picks stay put (sticky 30min); auto rules may enter/exit it
      // only while the current scene itself is autoSwitchable
      autoSwitchable: false,
      idle: "idle_morning",
      // ParamCheek10 = 18椭眼镜 (moc-default ON): sleepy morning goes bare-faced
      outfit: { love: 0, fangzhuoshang: 0, paopao: 0, Param73: 0, Param70: 0, Param71: 0, cc2: 0, ParamCheek70: 0, ParamCheek10: 0, phone: 0, phone2: 0 },
      stickers: [
        // pillow leans at the desk's left-front corner, starlamp on the right;
        // below:true is useless here — the desk mat is model art, it hides
        // anything under the model
        { file: "pillow", anchor: "body", dx: -1.55, dy: 1.5, scale: 1.1, rot: -14 },
        { file: "starlamp", anchor: "body", dx: 1.0, dy: 1.65, scale: 0.8 },
      ],
      line: "唔…再睡五分钟…就五分钟……",
      acts: {
        head: {
          motions: ["n_drowse"],
          expressions: ["脸红", "呆呆眼"],
          lines: ["唔喵…别吵…本鲸鱼娘还没醒……", "再睡五分钟…真的就五分钟……"],
        },
        body: {
          motions: ["n_wakeup"],
          expressions: ["生气", "问号"],
          lines: ["呀！谁把太阳开这么大！", "起床气也要讲究基本法的哦…"],
        },
        dblclick: {
          motions: ["h_jump"],
          expressions: ["开心兴奋"],
          lines: ["呜哇！彻底醒了啦！！"],
        },
        dragStart: {
          motions: ["n_float"],
          expressions: ["呆呆眼"],
          lines: ["唔……睡梦中飘起来了……"],
        },
        dragEnd: {
          motions: ["n_dream"],
          expressions: ["脸红"],
          lines: ["呼……继续睡……"],
        },
      },
    },
    drama: {
      label: "追剧时刻",
      autoSwitchable: false,
      idle: "idle_drama",
      outfit: { love: 0, fangzhuoshang: 0, phone: 1, phone2: 1, paopao: 0, Param73: 0, cc2: 0, ParamCheek70: 0, Param70: 0, Param71: 0 },
      stickers: [
        // snack at hand level (cheek level read as an earring)
        { file: "apple", anchor: "body", dx: 0.45, dy: 1.16, scale: 0.5 },
      ],
      line: "嘘——这段超关键！本鲸鱼娘看到要紧的地方了！",
      acts: {
        head: {
          motions: ["headpat"],
          expressions: ["调皮", "问号"],
          lines: ["呀、别挡光！这一幕超关键的啦！", "唔…看剧被摸头会错过糖点的哦"],
        },
        body: {
          motions: ["m_protect"],
          expressions: ["生气", "流汗"],
          lines: ["剧透警告！再戳就不告诉你后面剧情了哦", "呜、零食都被你戳掉了啦"],
        },
        dblclick: {
          motions: ["h_glee"],
          expressions: ["星星眼", "开心兴奋"],
          lines: ["啊啊啊这一段也太好了吧！！", "本鲸鱼娘宣布这就是本季度最佳！"],
        },
        dragStart: {
          motions: ["surprised"],
          expressions: ["惊讶"],
          lines: ["呀！手机要掉了要掉了！"],
        },
        dragEnd: {
          motions: ["shy"],
          expressions: ["脸红"],
          lines: ["呼…刚看到哪了来着…"],
        },
      },
    },
    night: {
      label: "深夜陪伴",
      autoSwitchable: true,
      idle: "idle_night",
      dim: 0.18,
      outfit: { cc2: 1, paopao: 0, Param70: 0, Param71: 0, phone: 0, phone2: 0, love: 0 },
      stickers: [{ file: "nightcap", anchor: "head", dx: 0, dy: -0.008, scale: 0.85, sway: 0, rot: -12, worn: true }],
      line: "夜深了…主人哥哥也早点休息，本鲸鱼娘陪你一会儿。",
      acts: {
        head: {
          motions: ["n_drowse", "headpat"],
          expressions: ["闭眼口水", "呆呆眼"],
          lines: ["唔唔…困…但是摸头很舒服…", "呼…呼…（睡得迷迷糊糊）"],
        },
        body: {
          motions: ["n_wakeup"],
          expressions: ["呆呆眼", "晕晕"],
          lines: ["唔哇！…啊，是主人哥哥啊，吓醒了", "熬夜的小鲸鱼，泡泡都吹不圆了…"],
        },
        dragStart: {
          motions: ["n_float"],
          expressions: ["晕晕", "闭眼口水"],
          lines: ["唔…漂浮…像在深海里睡觉…", "呼…呼…（被拎着也照睡）"],
        },
        dragEnd: {
          motions: ["n_dream", "shy"],
          expressions: ["晕晕", "呆呆眼"],
          lines: ["唔…落床了…晚安…", "呼…谢谢主人哥哥…轻放…"],
        },
        dblclick: {
          motions: ["n_dream"],
          expressions: ["呆呆眼", "问号"],
          lines: ["唔…两下…是在说晚安吗…", "梦里也在陪你哦…呼…"],
        },
      },
      micro: ["n_drowse", "n_dream"],
    },
    devil: {
      label: "小恶魔",
      autoSwitchable: false,
      idle: "idle_devil",
      outfit: { Param30: 1, Param31: 1, Param32: 1, paopao: 0, Param70: 0, Param71: 0, Param73: 0, phone: 0, phone2: 0, love: 0 },
      stickers: [
        { file: "batwings", anchor: "body", dx: 0, dy: 0.02, scale: 1.35, rot: 0, below: true },
        { file: "trident", anchor: "head", dx: 0.85, dy: 0.898, scale: 0.85, sway: 12, rot: 8 },
      ],
      line: "哼哼，本恶魔鲸今天心情好，就陪你玩玩吧。",
      acts: {
        head: {
          motions: ["d_smug", "headpat"],
          expressions: ["调皮", "阴暗"],
          lines: ["摸、摸恶魔的头？胆子不小嘛", "哼哼，看在你摸得舒服的份上，饶你一次"],
        },
        body: {
          motions: ["splash", "d_zap"],
          expressions: ["生气", "调皮"],
          lines: ["呀！再戳就喷你一脸水哦！", "敢戳本恶魔？胆子肥了！"],
        },
        dragStart: {
          motions: ["d_wingpanic"],
          expressions: ["生气", "感叹号"],
          lines: ["放、放手！恶魔的翅膀不是给你抓的！", "呜哇，翅膀乱了乱了！"],
        },
        dragEnd: {
          motions: ["d_smug", "shy"],
          expressions: ["阴暗", "调皮"],
          lines: ["哼，本恶魔记仇了…三秒后原谅你", "记住了哦，下次要给小鱼干才让拎"],
        },
        dblclick: {
          motions: ["d_mischief"],
          expressions: ["调皮", "星星眼"],
          lines: ["嘿嘿嘿，双击是恶魔契约，你签了哦", "契约成立！主人哥哥现在是本恶魔的专属人类了"],
        },
      },
      micro: ["d_laugh", "d_zap", "d_smug"],
    },
  };

  // Manual scene choice is sticky for a while: auto time switches only fire
  // when the current scene is auto-switchable or was itself auto-chosen.
  let sceneManualUntil = 0;
  // currentScene stays null until the first performScene applies the saved
  // scene (outfit + idle + stickers) after the model finishes loading.
  let currentScene = null;

  // Motion duration / loop metadata, filled from the motion3 files at startup
  // so one-shots can resume the looping idle automatically.
  const MOTION_DATA = {};

  let outfitMap = {};
  let dimOverlay = null;
  let pageVisible = true;
  let settleMap = null;
  let settleUntil = 0;
  let resumeTimer = 0;
  let microTimer = 0;
  let lastMotionGroup = null;

  // ------------------------------------------------------- sticker layer
  // Scene props (headphones / nightcap / ...) rendered as PIXI sprites that
  // are ANCHORED TO THE BODY: each frame we read the Live2D drawable vertex
  // positions of one representative head/torso mesh and place the sprite on
  // that anchor, so accessories follow every motion, scale with the model and
  // sway with a tiny spring.  Vertex reads are CPU-side Float32Array views of
  // one or two meshes - negligible next to the Live2D draw itself.  Missing
  // files are skipped once; scenes degrade to the param outfit only.
  const stickerSprites = {};
  const stickerFail = {};
  const anchors = { head: null, body: null, ready: false };

  function stickerTextureUrl(scene, file) {
    return `../assets/stickers/${scene}/${file}.png`;
  }

  function drawableBox(core, index) {
    const v = core.getDrawableVertexPositions(index);
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let j = 0; j < v.length; j += 2) {
      const x = v[j], y = v[j + 1];
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      // canvas y grows upward; track the visual top explicitly
      if (y > maxY) maxY = y;
      if (y < minY) minY = y;
    }
    return { minX, minY, maxX, maxY };
  }

  function unionBox(target, box) {
    target.minX = Math.min(target.minX, box.minX);
    target.minY = Math.min(target.minY, box.minY);
    target.maxX = Math.max(target.maxX, box.maxX);
    target.maxY = Math.max(target.maxY, box.maxY);
    return target;
  }

  // Pick representative head / torso meshes once (rest pose).  The tail and
  // desk are excluded by the horizontal-centre + width filters.
  function buildAnchors() {
    if (!model || anchors.ready) return;
    try {
      const core = model.internalModel.coreModel;
      const count = core.getDrawableCount();
      const all = [];
      const rawBoxes = [];
      for (let i = 0; i < count; i++) {
        if (core.getDrawableOpacity(i) < 0.05) continue;
        const box = drawableBox(core, i);
        rawBoxes.push({ i, box, id: String(core.getDrawableId(i) || "") });
      }
      if (!rawBoxes.length) return;
      all.push(...rawBoxes);
      // Percentile-robust reference box: flash overlays (exclaim marks,
      // hearts, tap bursts) sit above the head, and a build attempt mid-flash
      // used to inflate the span, compress every real topV below the head
      // threshold and lock an empty selection forever.  p98/p02 edges ignore
      // the few transient extremes while keeping the legit large pieces
      // (headdress/hair are several times the median mesh size, so median
      // caps would drop them).
      const sortedTops = rawBoxes.map((c) => c.box.maxY).sort((a, b) => a - b);
      const sortedBottoms = rawBoxes.map((c) => c.box.minY).sort((a, b) => a - b);
      const sortedLefts = rawBoxes.map((c) => c.box.minX).sort((a, b) => a - b);
      const sortedRights = rawBoxes.map((c) => c.box.maxX).sort((a, b) => a - b);
      const pct = (arr, q) => arr[Math.min(arr.length - 1, Math.max(0, Math.floor(q * arr.length)))];
      const modelBox = {
        minX: pct(sortedLefts, 0.02),
        minY: pct(sortedBottoms, 0.02),
        maxX: pct(sortedRights, 0.98),
        maxY: pct(sortedTops, 0.98),
      };
      const cx = (modelBox.minX + modelBox.maxX) / 2;
      const W = Math.max(0.2, modelBox.maxX - modelBox.minX);
      const H = Math.max(0.2, modelBox.maxY - modelBox.minY);
      // face centre x is the steadiest reference; the model bbox centre is
      // skewed by the tail and desk, which mislocated the bib onto the face
      let faceCx = cx;
      const faceIdBoxes = all.filter((c) => /face/i.test(c.id));
      if (faceIdBoxes.length) {
        const fb = faceIdBoxes[0].box;
        faceCx = (fb.minX + fb.maxX) / 2;
      }
      const centred = (b) => Math.abs((b.minX + b.maxX) / 2 - faceCx) < W * 0.15;
      const headBoxes = all
        .filter(({ box: b }) => {
          const topV = (b.maxY - modelBox.minY) / H;
          return centred(b) && topV > 0.62 && (b.maxX - b.minX) < W * 0.45;
        })
        .sort((a, b) => b.box.maxY - a.box.maxY)
        .slice(0, 5)
        .map((c) => c.i);
      const chestBoxes = all
        .filter(({ box: b }) => {
          const midV = ((b.maxY + b.minY) / 2 - modelBox.minY) / H;
          const bw = b.maxX - b.minX;
          return centred(b) && midV > 0.3 && midV < 0.58 && bw > W * 0.08 && bw < W * 0.38;
        })
        .sort((a, b) => (b.box.maxX - b.box.minX) - (a.box.maxX - a.box.minX))
        .slice(0, 3)
        .map((c) => c.i);
      if (headBoxes.length) {
        const faceIndices = headBoxes.filter((i) => /face/i.test(String(core.getDrawableId(i))));
        const chosenHead = faceIndices.length ? faceIndices : headBoxes;
        const headBox = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
        for (const i of chosenHead) unionBox(headBox, drawableBox(core, i));
        // the face mesh tops out at the eyebrows; accessories sit above the
        // hair, so expose a head-top that reaches the bangs
        headBox.topWithHair = headBox.maxY + (headBox.maxY - headBox.minY) * 0.35;
        // Pitch-tracking lid: among the head-area candidates the widest
        // non-face one is the headdress; its top edge rides every head pitch
        // while the face mesh barely moves.  liveAnchorBox consumes this.
        let lidIdx;
        let lidWidth = 0;
        for (const i of headBoxes) {
          if (/face/i.test(String(core.getDrawableId(i)))) continue;
          const b = drawableBox(core, i);
          const w = b.maxX - b.minX;
          if (w > lidWidth) {
            lidWidth = w;
            lidIdx = i;
          }
        }
        anchors.head = { indices: chosenHead, rest: headBox, lidIdx };
        // The head artwork (hair/headdress) slides as a whole on head pitch
        // while the face mesh barely moves, so head stickers anchor to the
        // headdress top (a rigid, pitch-tracking piece) with a STABLE span
        // measured here at rest, instead of the sliding face-derived span.
        anchors.head.restTopToBottom = headBox.topWithHair - headBox.minY;
        // stable width span too: the LIVE union box pumps during head
        // motions (hair sway widens it) which inflated stickers mid-motion
        anchors.head.restWidth = headBox.maxX - headBox.minX;
      }
      if (chestBoxes.length) {
        const bodyBox = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
        for (const i of chestBoxes) unionBox(bodyBox, drawableBox(core, i));
        anchors.body = { indices: chestBoxes, rest: bodyBox };
      }
      if (!headBoxes.length) {
        // transient state (crossfade deformation, flash overlay) — retry on
        // a later frame instead of locking an empty selection forever
        return;
      }
      anchors.ready = true;
      console.log("pet:anchors", JSON.stringify({ head: anchors.head && anchors.head.indices, body: anchors.body && anchors.body.indices }));
    } catch (error) {
      console.warn("pet:anchors failed", error);
    }
  }

  // Live anchor box in canvas coords (y-up), recomputed per frame from the
  // chosen drawables so the sticker rides every motion deformation.
  function liveAnchorBox(which) {
    const a = anchors[which];
    if (!a || !model) return null;
    const core = model.internalModel.coreModel;
    const box = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
    for (const i of a.indices) unionBox(box, drawableBox(core, i));
    if (box.minX === Infinity) return null;
    if (which === "head" && a.lidIdx !== undefined) {
      // headdress top rides every head pitch; the face box does not
      const lid = drawableBox(core, a.lidIdx);
      if (lid.maxX > lid.minX) {
        box.topWithHair = lid.maxY;
        if (lid.maxY > box.maxY) box.maxY = lid.maxY;
      }
    }
    return box;
  }

  const _stickerMatrix = new PIXI.Matrix();
  const _stickerPoint = new PIXI.Point();

  let _stickerPxPerUnit = 0;
  function canvasToScreen(x, y) {
    // pixi-live2d-display overrides the model container's transform update
    // (drawingMatrix is what the Cubism render consumes), so the inherited
    // worldTransform stays identity and CANNOT be used for mapping.  Compose
    // the chain from pieces that are provably current: centeringTransform
    // maps canvas units (y-up, hence the flip) into the model's internal
    // pixel space, then scale by on-screen/internal size and offset by the
    // model's anchored top-left corner on screen.
    const im = model.internalModel;
    _stickerMatrix.copyFrom(im.centeringTransform);
    const p = _stickerMatrix.apply(_stickerPoint.set(x, -y));
    const k = model.width / (im.width || model.width || 1);
    const gp = model.getGlobalPosition();
    const anchor = model.anchor || { x: 0.5, y: 1 };
    const topLeftX = gp.x - anchor.x * model.width;
    const topLeftY = gp.y - anchor.y * model.height;
    _stickerPxPerUnit = Math.abs(_stickerMatrix.d) * k || _stickerPxPerUnit;
    // fresh point per call: stickerFrame reads topLeft/topRight/height from
    // three consecutive calls, so a shared or alternating buffer would clobber
    return new PIXI.Point(topLeftX + p.x * k, topLeftY + p.y * k);
  }

  function ensureStickerSprites(scene) {
    const sceneDef = SCENES[scene];
    const wanted = sceneDef && sceneDef.stickers ? sceneDef.stickers : [];
    const active = [];
    for (const cfg of wanted) {
      const url = stickerTextureUrl(scene, cfg.file);
      if (stickerFail[url]) continue;
      let entry = stickerSprites[url];
      if (!entry) {
        if (!app) continue;
        try {
          const sprite = new PIXI.Sprite(PIXI.Texture.from(url));
          sprite.anchor.set(0.5, 0.5);
          sprite.alpha = 0;
          sprite.visible = false;
          // Texture.from failures are ASYNC: without this the skip-once
          // list never filled and broken stickers retried every frame
          sprite.texture.baseTexture.on("error", () => {
            stickerFail[url] = true;
            try { sprite.destroy(); } catch (_) {}
            delete stickerSprites[url];
          });
          if (cfg.below) app.stage.addChildAt(sprite, 0);
          else app.stage.addChild(sprite);
          entry = {
            sprite,
            cfg,
            scene,
            alpha: 0,
            target: 0,
            lastX: null,
            vel: 0,
          };
          stickerSprites[url] = entry;
        } catch (_) {
          stickerFail[url] = true;
          console.warn("pet:sticker load failed", url);
          continue;
        }
      }
      entry.target = 1;
      entry.cfg = cfg;
      active.push(entry);
    }
    // Fade out stickers that belong to other scenes.
    for (const url of Object.keys(stickerSprites)) {
      const entry = stickerSprites[url];
      if (!active.includes(entry)) entry.target = 0;
    }
    return active;
  }

  function stickerFrame(tickMs) {
    for (const entry of Object.values(stickerSprites)) {
      const sprite = entry.sprite;
      if (!model || (!anchors.ready && !buildAnchors())) {
        sprite.visible = false;
        continue;
      }
      // Asymmetric fade: leaving a scene must be quick — a previous scene's
      // sticker at near-full alpha reads as an identity bug (nightcap on
      // devil, trident on work, crown on music).  Entering stays gentle.
      const fadeRate = entry.target === 0 ? 0.32 : 0.12;
      entry.alpha += (entry.target - entry.alpha) * fadeRate;
      if (entry.target === 0 && entry.alpha < 0.3) entry.alpha = 0;
      if (entry.alpha < 0.02 && entry.target === 0) {
        sprite.visible = false;
        continue;
      }
      sprite.visible = true;
      sprite.alpha = entry.alpha;
      const cfg = entry.cfg;
      const box = liveAnchorBox(cfg.anchor || "head");
      if (!box) {
        sprite.visible = false;
        continue;
      }
      const topY = cfg.anchor !== "body" && box.topWithHair !== undefined ? box.topWithHair : box.maxY;
      const topLeft = canvasToScreen(box.minX, topY);
      const topRight = canvasToScreen(box.maxX, topY);
      let anchorW = Math.abs(topRight.x - topLeft.x);
      let anchorH = Math.abs(
        canvasToScreen(box.minX, box.minY).y - topLeft.y
      );
      const liveCx = (topLeft.x + topRight.x) / 2;
      if ((cfg.anchor || "head") === "head" && anchors.head) {
        // Stable spans measured at rest: the live union pumps during head
        // motions, which used to inflate/reposition stickers mid-motion.
        if (anchors.head.restWidth) {
          anchorW = anchors.head.restWidth * _stickerPxPerUnit;
        }
        if (anchors.head.restTopToBottom) {
          anchorH = anchors.head.restTopToBottom * _stickerPxPerUnit;
        }
      }
      // horizontal: ride the LIVE box center, offset by dx in rest-width units
      const px = liveCx + (cfg.dx || 0) * anchorW;
      const py = topLeft.y + (cfg.dy || 0) * anchorH;
      // spring: lean against anchor movement (body/head swing) + idle bob
      if (entry.lastX !== null) {
        const vx = px - entry.lastX;
        entry.vel += (vx - entry.vel) * 0.18;
      }
      entry.lastX = px;
      // worn accessories (headwear/bib) stay rigid on the anchor; only held
      // props (balloon/trident) get the sway spring, which otherwise shifted
      // the attachment point by up to ~20px during mouse sweeps
      const rigid = cfg.spring === false || !!cfg.worn;
      const lean = rigid ? 0 : Math.max(-0.12, Math.min(0.12, entry.vel * 0.008));
      const bob = rigid ? 0 : Math.sin(tickMs / 700 + 1.3) * 1.5 * ((cfg.sway || 0) / 8);
      let headTilt = 0;
      let pitchSlide = 0;
      if ((cfg.anchor || "head") === "head") {
        try {
          const coreModel = model.internalModel.coreModel;
          headTilt = (coreModel.getParameterValueById("ParamAngleZ") || 0) * 0.012;
          // head pitch: bowing tips the head top forward — rotate headwear
          // with it and let it slide along the hair slightly, so it stays
          // on through bows instead of lifting off the head
          const pitch = -(coreModel.getParameterValueById("ParamAngleY") || 0);
          headTilt += pitch * 0.022;
          pitchSlide = Math.sin((pitch * Math.PI) / 180) * 0.06;
        } catch (_) {}
      }
      sprite.rotation =
        ((cfg.rot || 0) * Math.PI) / 180 +
        headTilt +
        lean +
        (rigid ? 0 : Math.sin(tickMs / 900) * 0.014 * ((cfg.sway || 0) / 6));
      sprite.position.set(px, py + bob + pitchSlide * anchorH);
      const width = (cfg.scale || 0.6) * anchorW;
      const tex = sprite.texture;
      const aspect = tex && tex.width ? tex.height / tex.width : 1;
      sprite.width = width;
      sprite.height = width * aspect; // honor source aspect (legacy square stickers unaffected)
    }
  }

  function pick(list) {
    return list[Math.floor(Math.random() * list.length)];
  }

  function applyParams(map) {
    if (!model) return;
    const core = model.internalModel && model.internalModel.coreModel;
    if (!core || !map) return;
    for (const id of Object.keys(map)) {
      try {
        core.setParameterValueById(id, map[id]);
      } catch (_) {
        /* unknown param: skip */
      }
    }
  }

  // Effect params that motions/expressions write but no outfit map owns:
  // hearts (Param9 is the display master switch, love/aixing only style it)
  // and the bubble family.  Without this they leak across scenes.
  const FX_RESET = {
    love: 0, Param9: 0, aixing: 0,
    paopao: 0, paopao2: 0, paopao3: 0, paopao4: 0, paopao5: 0,
  };

  function switchOutfit(nextScene) {
    const next = (nextScene && nextScene.outfit) || {};
    const reset = {};
    for (const id of Object.keys(outfitMap)) {
      if (!(id in next)) reset[id] = 0;
    }
    for (const id of Object.keys(FX_RESET)) {
      if (!(id in next)) reset[id] = FX_RESET[id];
    }
    applyParams(reset);
    outfitMap = Object.assign({}, next);
    applyParams(outfitMap);
    // bake: the next idle's loadParameters() must not resurrect the FX
    try {
      model.internalModel.coreModel.saveParameters();
    } catch (_) {}
  }

  // Reassert outfit params every frame; cheap (a dozen ids) and bulletproof
  // against motions/expressions that happen to write the same ids.  Also
  // drives the sticker layer follow/fade per frame.
  function outfitTick() {
    window.__tickN = (window.__tickN || 0) + 1;
    if (!pageVisible) {
      requestAnimationFrame(outfitTick);
      return;
    }
    try {
      if (model) {
        if (Object.keys(outfitMap).length) applyParams(outfitMap);
        if (settleMap && performance.now() < settleUntil) applyParams(settleMap);
      }
      stickerFrame(performance.now());
    } catch (error) {
      // never let one bad frame kill the loop; surface the cause for diagnosis
      window.__tickErr = window.__tickErr || [];
      if (window.__tickErr.length < 5) {
        window.__tickErr.push(String((error && error.stack) || error).slice(0, 300));
      }
    }
    if (dimOverlay) {
      const dimTarget = (SCENES[currentScene] && SCENES[currentScene].dim) || 0;
      dimOverlay.alpha += (dimTarget - dimOverlay.alpha) * 0.06;
    }
    requestAnimationFrame(outfitTick);
  }

  function playExpression(name) {
    if (!model || !name) return;
    try {
      model.expression(name);
    } catch (_) {
      /* expression is optional */
    }
  }

  function resetExpression() {
    if (!model) return;
    try {
      model.internalModel.motionManager.expressionManager.resetExpression();
    } catch (_) {
      /* optional */
    }
  }

  function motionDuration(group, fallback) {
    const defs = MOTION_DATA[group];
    if (!defs || !defs.length) return fallback;
    return defs[0].duration;
  }

  // Start a motion at FORCE priority.  The default NORMAL priority deadlocks
  // behind a looping idle (MotionState.reserve rejects equal-or-lower priority
  // while the stock idle never finishes), so every scene/idle/one-shot start
  // here preempts explicitly.  Starts are SERIALIZED through a promise chain
  // with a sequence token: two scene switches racing each other would other-
  // wise resolve their motion loads out of order and the older one would win
  // (queueManager.startMotion stops every other motion when it lands).
  let captureMode = false; // freezes time-switches and micro-actions during screenshot runs
  let motionSeq = 0;
  let motionChain = Promise.resolve(true);

  function queueMotionStart(group) {
    lastMotionGroup = group;
    const seq = ++motionSeq;
    motionChain = motionChain.then(() => startMotionWithRetry(group, seq));
    return motionChain;
  }

  async function startMotionWithRetry(group, seq) {
    if (!model || !group) return false;
    const mm = model.internalModel.motionManager;
    let ok = false;
    try {
      ok = await mm.startMotion(group, 0, 3);
    } catch (_) {
      ok = false;
    }
    if (ok || seq !== motionSeq) return ok;
    await new Promise((resolve) => setTimeout(resolve, 250));
    if (seq !== motionSeq) return false;
    try {
      ok = await mm.startMotion(group, 0, 3);
    } catch (_) {
      ok = false;
    }
    return ok;
  }

  function resumeIdle() {
    interactionBusy = false;
    const scene = SCENES[currentScene];
    if (!scene || !model) return;
    resetExpression();
    const cleanup = cleanupFor(lastMotionGroup, scene.idle, Object.keys(outfitMap));
    if (cleanup) {
      settleMap = cleanup;
      settleUntil = performance.now() + 900;
      applyParams(cleanup);
      // bake so the idle motion's loadParameters() cannot resurrect props
      try { model.internalModel.coreModel.saveParameters(); } catch (_) {}
    }
    queueMotionStart(scene.idle);
  }

  // Play a one-shot motion and schedule the scene idle to resume afterwards.
  function playOneShot(group, fallbackDuration) {
    if (!model || !group) return;
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = 0;
    }
    const dur = Math.max(0.6, motionDuration(group, fallbackDuration || 1.5));
    resumeTimer = setTimeout(() => {
      resumeTimer = 0;
      resumeIdle();
    }, Math.round(dur * 1000) + 250);
    queueMotionStart(group);
  }

  function playMotion(name) {
    if (!model || !name) return;
    const defs = MOTION_DATA[name];
    if (defs && defs.length && defs[0].loop) {
      // Looping motion: it becomes the current idle.
      if (resumeTimer) {
        clearTimeout(resumeTimer);
        resumeTimer = 0;
      }
    }
    queueMotionStart(name);
  }

  function playAct(key) {
    const scene = SCENES[currentScene];
    // The tray menu labels the body interaction 戳一戳; scenes register it as
    // "body" — map the menu wording onto the scene key instead of dying.
    if (key === "poke" && !(scene && scene.acts && scene.acts.poke)) key = "body";
    const act = (scene && scene.acts && scene.acts[key]) || GENERIC_ACTS[key];
    if (!model || !act || interactionBusy) return;
    interactionBusy = true;
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = 0;
    }
    playOneShot(pick(act.motions || [act.motion]), 1.8);
    const expressions = act.expressions || [act.expression];
    if (expressions && expressions.length) playExpression(pick(expressions));
    showBubble(pick(act.lines));
  }

  function performScene(name, { manual = false } = {}) {
    const scene = SCENES[name];
    if (!model || !scene || name === currentScene) {
      if (scene && name === currentScene) showBubble(scene.line);
      return;
    }
    if (manual) {
      sceneManualUntil = Date.now() + 30 * 60 * 1000;
      localStorage.setItem("pet.scene", name);
    }
    switchOutfit(scene);
    ensureStickerSprites(name);
    const cleanup = cleanupFor(currentScene ? SCENES[currentScene].idle : null, scene.idle, Object.keys(outfitMap));
    if (cleanup) {
      settleMap = cleanup;
      settleUntil = performance.now() + 900;
      applyParams(cleanup);
      // bake so the next idle's loadParameters() cannot resurrect old props
      try { model.internalModel.coreModel.saveParameters(); } catch (_) {}
    }
    currentScene = name;
    interactionBusy = false;
    if (resumeTimer) {
      clearTimeout(resumeTimer);
      resumeTimer = 0;
    }
    try {
      playMotion(scene.idle);
    } catch (_) {}
    resetExpression();
    showBubble(scene.line);
    console.log("pet:scene", name);
  }

  function scheduleMicro() {
    if (microTimer) clearTimeout(microTimer);
    const delay = 10000 + Math.random() * 15000;
    microTimer = setTimeout(() => {
      microTimer = 0;
      if (captureMode) {
        scheduleMicro();
        return;
      }
      const scene = SCENES[currentScene];
      if (
        scene &&
        scene.micro &&
        scene.micro.length &&
        !interactionBusy &&
        !dragging &&
        !dragCandidate
      ) {
        playOneShot(pick(scene.micro), 1.5);
      }
      scheduleMicro();
    }, delay);
  }

  // ------------------------------------------------------ time behaviors

  function checkTimeBehaviors() {
    if (captureMode) return;
    if (localStorage.getItem("pet.timeAuto") === "0") return;
    const now = new Date();
    const today = now.toISOString().slice(0, 10);
    const hour = now.getHours() + now.getMinutes() / 60;
    const sticky = Date.now() < sceneManualUntil;
    const scene = SCENES[currentScene];
    const canAuto = scene && (scene.autoSwitchable || !sticky);
    // Late night: switch to the sleepy scene (23:00 - 06:00).
    if (canAuto && currentScene !== "night" && (hour >= 23 || hour < 6)) {
      performScene("night");
      return;
    }
    // Morning: hand the sleepy scene back to the default one.
    if (canAuto && currentScene === "night" && hour >= 6 && hour < 8.5) {
      performScene("default");
      return;
    }
    // Weekend mornings: she stays in bed (auto in, auto out at 11:00)
    const weekendMorning = (now.getDay() === 0 || now.getDay() === 6) && hour >= 8.5 && hour < 11;
    if (canAuto && currentScene !== "morning" && weekendMorning && currentScene !== "night") {
      performScene("morning");
      return;
    }
    if (canAuto && currentScene === "morning" && !weekendMorning) {
      performScene("default");
      return;
    }
    // Drama time hint: evenings, once a day, only from the desk scenes
    if (hour >= 20 && hour < 23 && localStorage.getItem("pet.dramaHintDate") !== today
        && (currentScene === "default" || currentScene === "work")) {
      localStorage.setItem("pet.dramaHintDate", today);
      showBubble("要看剧吗？本鲸鱼娘备好零食了哦～右键点「追剧时刻」！");
    }
    // Meal hint: nudge once per window around lunch / dinner, no auto switch.
    const isMealtime = (hour >= 11.5 && hour < 13) || (hour >= 17.5 && hour < 19);
    if (isMealtime && localStorage.getItem("pet.mealHintDate") !== today) {
      localStorage.setItem("pet.mealHintDate", today);
      if (currentScene !== "meal") {
        showBubble("到饭点啦！本鲸鱼娘饿了…右键点「干饭时间」，一起好好吃饭哦！");
      }
    }
  }

  function startTimeBehaviors() {
    checkTimeBehaviors();
    setInterval(checkTimeBehaviors, 60 * 1000);
  }

  let audioCtx = null;
  let analyser = null;
  let mouthRaf = 0;
  let currentAudio = null;
  let audioEl = null;
  let audioSource = null;
  let currentUrl = null;

  function setMouth(value) {
    if (!model) return;
    try {
      model.internalModel.coreModel.setParameterValueById(
        "ParamMouthOpenY",
        Math.max(0, Math.min(1, value))
      );
    } catch (_) {
      /* model without mouth parameter */
    }
  }

  function stopMouth() {
    if (mouthRaf) cancelAnimationFrame(mouthRaf);
    mouthRaf = 0;
    setMouth(0);
  }

  async function speak(text) {
    if (!text) return;
    try {
      if (currentAudio) {
        currentAudio.pause(); // interrupt: the src swap below reuses the element
      }
      const response = await fetch("/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, voice: "soft" }),
      });
      if (!response.ok) return;
      const blob = await response.blob();
      if (currentUrl) URL.revokeObjectURL(currentUrl); // no blob buildup
      currentUrl = URL.createObjectURL(blob);
      if (!audioEl) {
        audioEl = new Audio();
        currentAudio = audioEl;
        if (!audioCtx) {
          audioCtx = new (window.AudioContext || window.webkitAudioContext)();
          analyser = audioCtx.createAnalyser();
          analyser.fftSize = 512;
        }
        if (audioCtx.state === "suspended") await audioCtx.resume();
        // one MediaElementSource for the element's lifetime: re-creating it
        // per speak() leaked the element + graph node every reply
        audioSource = audioCtx.createMediaElementSource(audioEl);
        audioSource.connect(analyser);
        analyser.connect(audioCtx.destination);
      }
      const audio = audioEl;
      audio.src = currentUrl;
      const data = new Uint8Array(analyser.fftSize);
      const tick = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i += 1) {
          const v = (data[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / data.length);
        setMouth(Math.min(1, rms * 5.5));
        mouthRaf = requestAnimationFrame(tick);
      };
      audio.onended = () => {
        stopMouth();
        URL.revokeObjectURL(currentUrl);
        currentUrl = null;
      };
      audio.onerror = () => {
        stopMouth();
      };
      await audio.play();
      stopMouth();
      tick();
    } catch (error) {
      console.warn("tts failed", error);
      stopMouth();
    }
  }

  function setConn(text, ok) {
    conn.textContent = text;
    conn.style.color = ok ? "rgba(150,255,190,.9)" : "rgba(255,210,160,.9)";
  }

  function positionBubble() {
    if (!model || !bubble || bubble.classList.contains("hidden")) return;
    try {
      const bounds = model.getBounds();
      const height = bubble.offsetHeight || 40;
      const top = Math.max(6, bounds.y - height - 10);
      bubble.style.top = `${Math.round(top)}px`;
    } catch (_) {
      /* keep the CSS fallback position */
    }
  }

  function showBubble(text, { user = false, keep = false } = {}) {
    if (!text) return;
    bubble.classList.remove("hidden");
    bubble.textContent = text;
    bubble.style.background = user ? "rgba(205,228,255,.92)" : "rgba(255,255,255,.92)";
    if (bubbleTimer) clearTimeout(bubbleTimer);
    if (!keep) {
      bubbleTimer = setTimeout(() => bubble.classList.add("hidden"), 14000);
    }
    requestAnimationFrame(positionBubble);
  }

  function applyScale() {
    if (!model) return;
    const width = Math.max(1, baseModelWidth);
    const height = Math.max(1, baseModelHeight);
    const fit = Math.min(
      (window.innerWidth * 0.92) / width,
      (window.innerHeight * 0.88) / height
    );
    model.scale.set(fit);
    model.x = window.innerWidth / 2;
    model.y = window.innerHeight - 8;
    // Scale the whole page visually against a fixed Electron window.  Resizing
    // the transparent frameless BrowserWindow on Windows was observed to grow
    // the window by 1px per drag/size operation, which looked like the entire
    // pet (top bar + dialog + model) was growing.
    document.body.style.transform = `scale(${modelScale})`;
    document.body.style.transformOrigin = "50% 100%";
    document.documentElement.style.setProperty("--ui-scale", "1");
    if (zoomLabel) zoomLabel.textContent = `${Math.round(modelScale * 100)}%`;
    if (sizeLabel) sizeLabel.textContent = `${window.innerWidth}×${window.innerHeight}`;
    requestAnimationFrame(positionBubble);
  }

  function setModelScale(value) {
    const next = Number(value);
    modelScale = Number.isFinite(next) ? Math.max(0.45, Math.min(1.35, next)) : 1;
    localStorage.setItem("pet.modelScale", String(modelScale));
    applyScale();
    console.log("pet:scale", modelScale.toFixed(2));
  }

  function toCanvasPoint(clientX, clientY) {
    const rect = stage.getBoundingClientRect();
    const scale = modelScale || 1;
    return [(clientX - rect.left) / scale, (clientY - rect.top) / scale];
  }

  function applyWindowOpacity(value) {
    windowOpacity = Math.max(20, Math.min(100, Number(value) || 100));
    localStorage.setItem("pet.opacity", String(windowOpacity));
    if (opacity) opacity.value = String(windowOpacity);
    if (window.petWindow && window.petWindow.setOpacity) {
      window.petWindow.setOpacity(windowOpacity / 100);
    }
  }

  function resize() {
    if (dimOverlay && app) {
      dimOverlay.clear().beginFill(0x0a0e2a)
        .drawRect(0, 0, app.renderer.screen.width, app.renderer.screen.height)
        .endFill();
    }
    if (!app) return;
    app.renderer.resize(window.innerWidth, window.innerHeight);
    applyScale();
  }

  function playInteraction(hitNames, clientX, clientY) {
    if (!model || interactionBusy) return;
    const head = (hitNames || []).some((name) => /head|头/i.test(name));
    const body = (hitNames || []).some((name) => /body|身/i.test(name));
    if (!head && !body) return;
    playAct(head ? "head" : "body");
  }

  function playInteractionAt(hitNames, clientX, clientY) {
    if (!model) return;
    let names = Array.isArray(hitNames) ? hitNames.slice() : [];
    if (!names.some((name) => /head|头|body|身/i.test(name))) {
      // Fallback when the model lacks usable hit-test geometry: use the
      // model's on-screen bounds so a body click still reacts.
      const [x, y] = toCanvasPoint(clientX, clientY);
      const bounds = model.getBounds();
      if (
        x < bounds.x ||
        y < bounds.y ||
        x > bounds.x + bounds.width ||
        y > bounds.y + bounds.height
      ) {
        return;
      }
      const rel = (y - bounds.y) / Math.max(1, bounds.height);
      names = [rel < 0.55 ? "Head" : "Body"];
    }
    playInteraction(names, clientX, clientY);
  }

  function isInteractiveAt(x, y) {
    const element = document.elementFromPoint(x, y);
    if (!element) return false;
    if (element.closest("#controls, #inputbar, .bubble, .dragbar")) return true;
    if (!model) return false;
    if (!element.closest("#stage")) return false;
    const [cx, cy] = toCanvasPoint(x, y);
    try {
      if (model.hitTest(cx, cy).length) return true;
    } catch (_) {
      /* fall through to bounds */
    }
    const bounds = model.getBounds();
    return (
      cx >= bounds.x &&
      cy >= bounds.y &&
      cx <= bounds.x + bounds.width &&
      cy <= bounds.y + bounds.height
    );
  }

  function updateClickThrough(x, y) {
    if (dragging || dragCandidate) return;
    const interactive = isInteractiveAt(x, y);
    if (interactive === lastInteractive) return;
    lastInteractive = interactive;
    if (window.petWindow && window.petWindow.setIgnore) {
      window.petWindow.setIgnore(!interactive);
    }
  }

  // Parameters each motion group curves; used to reset props/fx that a
  // finished motion leaves behind (hammer, hearts, cat ears...).
  let paramDefaults = null; // id -> moc default value, snapshotted before any motion

  async function loadMotionMeta() {
    try {
      const res = await fetch("../assets/model/c_0120.model3.json");
      const manifest = await res.json();
      const motions = (manifest.FileReferences && manifest.FileReferences.Motions) || {};
      const jobs = [];
      for (const group of Object.keys(motions)) {
        for (const def of motions[group]) {
          const file = String(def.File || "");
          jobs.push(
            fetch(`../assets/model/${file}`)
              .then((r) => r.json())
              .then((data) => {
                const meta = data.Meta || {};
                MOTION_DATA[group] = [
                  {
                    file,
                    duration: Number(meta.Duration) || 2,
                    loop: Boolean(meta.Loop),
                    curveIds: (data.Curves || []).map((c) => c.Id),
                  },
                ];
              })
              .catch(() => {})
          );
        }
      }
      jobs.push(
        fetch("../assets/model/c_0120.cdi3.json")
          .then((r) => r.json())
          .then((cdi) => {
            const core = model.internalModel.coreModel;
            paramDefaults = {};
            for (const p of cdi.Parameters || []) {
              try {
                paramDefaults[p.Id] = core.getParameterValueById(p.Id);
              } catch (_) {}
            }
          })
          .catch(() => {})
      );
      await Promise.all(jobs);
    } catch (error) {
      console.warn("motion meta load failed", error);
    }
  }

  function curveIdsOf(group) {
    const defs = MOTION_DATA[group];
    return defs && defs[0] && defs[0].curveIds ? defs[0].curveIds : null;
  }

  // Params the old motion curved that the new one does not: reset them to the
  // moc defaults so props/effects do not leak across switches.
  function cleanupFor(oldGroup, newGroup, extraKeep) {
    if (!paramDefaults) return null;
    const oldIds = curveIdsOf(oldGroup);
    const newIds = curveIdsOf(newGroup);
    if (!oldIds) return null;
    const keep = new Set(newIds || []);
    for (const id of extraKeep || []) keep.add(id);
    const map = {};
    for (const id of oldIds) {
      if (!keep.has(id) && id in paramDefaults) map[id] = paramDefaults[id];
    }
    return Object.keys(map).length ? map : null;
  }

  async function loadModel() {
    app = new PIXI.Application({
      resizeTo: window,
      transparent: true,
      backgroundAlpha: 0,
      antialias: true,
      autoStart: true,
      preserveDrawingBuffer: true, // QA capture hooks read the full-window canvas
    });
    stage.appendChild(app.view);
    app.ticker.maxFPS = 60; // 144Hz panels do not need 144 Live2D renders
    window.addEventListener("resize", resize);
    // hidden windows render nothing — stop the ticker until shown again
    if (window.petWindow && window.petWindow.onVisibility) {
      window.petWindow.onVisibility((visible) => {
        pageVisible = !!visible;
        if (!app) return;
        if (pageVisible) app.ticker.start();
        else app.ticker.stop();
      });
    }

    try {
      model = await PIXI.live2d.Live2DModel.from("../assets/model/c_0120.model3.json", {
        autoInteract: false,
      });
      model.anchor.set(0.5, 1);
      app.stage.addChild(model);
      // night-scene dimming sits above the model mesh, below all stickers
      dimOverlay = new PIXI.Graphics();
      dimOverlay.beginFill(0x0a0e2a)
        .drawRect(0, 0, app.renderer.screen.width || 1280, app.renderer.screen.height || 720)
        .endFill();
      dimOverlay.alpha = 0;
      app.stage.addChild(dimOverlay);
      // stickers may have been added while the model was still loading; re-add
      // them so every sticker renders ABOVE the model mesh regardless of order
      for (const entry of Object.values(stickerSprites)) {
        if (entry.cfg && entry.cfg.below) app.stage.addChildAt(entry.sprite, 0);
        else app.stage.addChild(entry.sprite);
      }
      baseModelWidth = Number(model.internalModel.originalWidth) || Number(model.internalModel.width) || model.width;
      baseModelHeight = Number(model.internalModel.originalHeight) || Number(model.internalModel.height) || model.height;
      resize();
      await loadMotionMeta();
      // First scene application goes through performScene so the saved scene's
      // outfit, idle motion and stickers all land, not just the idle.
      const savedScene = localStorage.getItem("pet.scene") || "default";
      performScene(SCENES[savedScene] ? savedScene : "default");
      outfitTick();
      scheduleMicro();
      startTimeBehaviors();
      window.addEventListener("pointermove", (event) => {
        if (!model) return;
        try {
          const [cx, cy] = toCanvasPoint(event.clientX, event.clientY);
          model.focus(cx, cy);
        } catch (_) {
          /* focus is best-effort */
        }
        updateClickThrough(event.clientX, event.clientY);
      });
      window.addEventListener("mousemove", (event) => {
        updateClickThrough(event.clientX, event.clientY);
      });
      window.addEventListener("mouseleave", () => {
        if (dragging || dragCandidate) return;
        lastInteractive = false;
        if (window.petWindow && window.petWindow.setIgnore) {
          window.petWindow.setIgnore(true);
        }
      });
      stage.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !model) return;
        // Only a press on the visible model starts a possible window drag.
        // Empty transparent area is handled by click-through in the main process.
        if (!isInteractiveAt(event.clientX, event.clientY)) return;
        let hits = [];
        try {
          const [cx, cy] = toCanvasPoint(event.clientX, event.clientY);
          hits = model.hitTest(cx, cy) || [];
        } catch (_) {
          hits = [];
        }
        event.preventDefault();
        if (window.petWindow && window.petWindow.setIgnore) {
          window.petWindow.setIgnore(false);
        }
        dragCandidate = {
          hits,
          startX: event.screenX,
          startY: event.screenY,
          pointerId: event.pointerId,
          clientX: event.clientX,
          clientY: event.clientY,
        };
        dragging = false;
        dragArmed = false;
        if (dragTimer) clearTimeout(dragTimer);
        try {
          stage.setPointerCapture(event.pointerId);
        } catch (_) {}
        // Do not start a window drag on pointerdown.  A normal click often
        // emits 1-3px of pointermove; the old immediate dragStart made the
        // whole window move/enlarge on every click.  Arm dragging only after
        // a short hold; quick clicks stay clicks.
        dragTimer = setTimeout(() => {
          dragTimer = 0;
          if (!dragCandidate) return;
          dragArmed = true;
          if (window.petWindow) window.petWindow.dragStart();
        }, 220);
        console.log("pet:pointerdown", JSON.stringify(hits));
      });

      window.addEventListener("pointermove", (event) => {
        if (!model || !dragCandidate) return;
        const dx = event.screenX - dragCandidate.startX;
        const dy = event.screenY - dragCandidate.startY;
        if (!dragArmed) {
          // A quick movement means this is a click/drag-attempt, not a
          // deliberate hold-to-drag; drop the candidate so it cannot move
          // the window from a click.
          if (Math.hypot(dx, dy) > 12) {
            if (dragTimer) {
              clearTimeout(dragTimer);
              dragTimer = 0;
            }
            dragCandidate = null;
          }
          return;
        }
        if (!dragging && Math.hypot(dx, dy) > 2) {
          dragging = true;
          interactionBusy = true;
          playAct("dragStart");
        }
        if (dragging && window.petWindow) window.petWindow.dragMove();
      });

      const finishPointer = (event) => {
        if (!dragCandidate) return;
        const candidate = dragCandidate;
        const wasDragging = dragging;
        const wasArmed = dragArmed;
        dragCandidate = null;
        dragging = false;
        dragArmed = false;
        if (dragTimer) {
          clearTimeout(dragTimer);
          dragTimer = 0;
        }
        try {
          stage.releasePointerCapture(candidate.pointerId);
        } catch (_) {}
        if (wasArmed && window.petWindow) window.petWindow.dragEnd();
        if (wasDragging) {
          playAct("dragEnd");
          setTimeout(() => {
            interactionBusy = false;
          }, 1500);
        } else {
          // Double click on the model triggers the scene's special act.
          const now = Date.now();
          const near =
            Math.hypot(
              event.clientX - lastClickPos[0],
              event.clientY - lastClickPos[1]
            ) < 28;
          if (now - lastClickAt < 400 && near) {
            lastClickAt = 0;
            playAct("dblclick");
          } else {
            lastClickAt = now;
            lastClickPos = [event.clientX, event.clientY];
            playInteractionAt(candidate.hits, event.clientX, event.clientY);
          }
        }
        updateClickThrough(event.clientX, event.clientY);
      };
      window.addEventListener("pointerup", finishPointer);
      window.addEventListener("pointercancel", finishPointer);
      // Zoom is deliberately button/keyboard only.  A trackpad/wheel with
      // Ctrl can emit dozens of events in a few ms; that was the "keeps
      // getting bigger" report.  Ctrl+= / Ctrl+- / Ctrl+0 and the visible
      // buttons are rate-limited by human input.
      window.addEventListener("keydown", (event) => {
        if (!event.ctrlKey || !model) return;
        const key = event.key;
        const code = event.code;
        if (key === "=" || key === "+" || code === "Equal" || code === "NumpadAdd") {
          event.preventDefault();
          setModelScale(modelScale + 0.1);
        } else if (key === "-" || key === "_" || code === "Minus" || code === "NumpadSubtract") {
          event.preventDefault();
          setModelScale(modelScale - 0.1);
        } else if (key === "0" || code === "Digit0" || code === "Numpad0") {
          event.preventDefault();
          setModelScale(1);
        }
      });

      zoomIn.addEventListener("click", () => setModelScale(modelScale + 0.1));
      zoomOut.addEventListener("click", () => setModelScale(modelScale - 0.1));
      if (zoomLabel) zoomLabel.addEventListener("click", () => setModelScale(1));

      window.addEventListener("contextmenu", (event) => {
        if (event.target && event.target.closest && event.target.closest("#msg")) {
          return; // keep native paste menu for the text field
        }
        event.preventDefault();
        if (window.petWindow && window.petWindow.contextMenu) {
          window.petWindow.contextMenu({ x: event.screenX, y: event.screenY });
        }
      });

      if (window.petWindow && window.petWindow.onCommand) {
        window.petWindow.onCommand((command) => {
          if (command.startsWith("action:")) {
            playAct(command.slice("action:".length));
          } else if (command.startsWith("scene:")) {
            performScene(command.slice("scene:".length), { manual: true });
          } else if (command === "zoom:in") {
            setModelScale(modelScale + 0.1);
          } else if (command === "zoom:out") {
            setModelScale(modelScale - 0.1);
          } else if (command === "zoom:reset") {
            setModelScale(1);
          }
        });
      }

      opacity.value = String(windowOpacity);
      opacity.addEventListener("input", () => applyWindowOpacity(opacity.value));
      applyWindowOpacity(windowOpacity);
      window.__petDebug = {
        model: () => model,
        scale: () => modelScale,
        base: () => [baseModelWidth, baseModelHeight],
        scenes: () => SCENES,
        motionData: () => MOTION_DATA,
        scene: () => currentScene,
        setScene: (name) => performScene(name, { manual: true }),
        act: (key) => playAct(key),
        anchors: () => anchors,
        liveAnchorBox: (w) => liveAnchorBox(w),
        app: () => app,
        motion: (name) => playMotion(name),
        oneshot: (name) => playOneShot(name, 2),
        freeze: (v) => { captureMode = !!v; },
        captureMotion: async (opts) => {
          const o = opts || {};
          const group = o.group;
          const im = model.internalModel;
          const mm = im.motionManager;
          const core = im.coreModel;
          const dt = 33;
          let now = performance.now();
          // drop props/fx the previous capture left behind (hammer, hearts...)
          const preClean = cleanupFor(lastMotionGroup, group, Object.keys(outfitMap));
          if (preClean) applyParams(preClean);
          for (let i = 0; i < 2; i++) {
            now += dt;
            im.update(dt, now);
            applyParams(outfitMap);
            if (preClean) applyParams(preClean);
            stickerFrame(now);
          }
          // CubismMotion restores its saved-parameter buffer on every update,
          // resurrecting values from the previous motion.  Bake the cleaned
          // state into the buffer or props leak straight through.
          if (preClean && core.saveParameters) core.saveParameters();
          const base = now;
          let started = false;
          try { started = await mm.startMotion(group, 0, 3); } catch (_) { started = false; }
          lastMotionGroup = group;
          const qm = mm.queueManager;
          const entry = (qm._motions || [])[qm._motions.length - 1];
          if (entry) entry._startTimeSeconds = base / 1000; // align queue clock with the driven clock
          const dur = Number(o.duration) || motionDuration(group, 2);
          resetExpression();
          if (o.expression) playExpression(o.expression);
          const watchIds = o.params || [];
          const fractions = o.fractions || [0.25, 0.5, 0.75, 0.92];
          const out = [];
          const watched = [];
          for (const f of fractions) {
            const target = base + dur * 1000 * f;
            while (now < target) {
              now += dt;
              im.update(dt, now);
              applyParams(outfitMap);
              stickerFrame(now);
            }
            if (watchIds.length) {
              watched.push(watchIds.map((id) => Math.round(core.getParameterValueById(id) * 100) / 100));
            }
            app.renderer.render(app.stage);
            out.push(app.renderer.view.toDataURL("image/jpeg", o.quality || 0.8));
          }
          return {
            frames: out,
            watched,
            started,
            stateGroup: mm.state ? mm.state.currentGroup : null,
          };
        },
        captureScene: async (opts) => {
          const o = opts || {};
          const im = model.internalModel;
          const mm = im.motionManager;
          const dt = 33;
          let now = performance.now();
          for (let i = 0; i < 2; i++) {
            now += dt;
            im.update(dt, now);
            applyParams(outfitMap);
            stickerFrame(now);
          }
          const base = now;
          try { await mm.startMotion((SCENES[o.scene] || {}).idle || "idle", 0, 3); } catch (_) {}
          const qm = mm.queueManager;
          const entry = (qm._motions || [])[qm._motions.length - 1];
          if (entry) entry._startTimeSeconds = base / 1000;
          const extract = app.renderer.extract || (app.renderer.plugins && app.renderer.plugins.extract);
          const out = [];
          const stations = o.stations || [];
          for (const st of stations) {
            // settle a few frames at the station so focus smoothing converges
            for (let k = 0; k < 6; k++) {
              now += dt;
              im.update(dt, now);
              applyParams(outfitMap);
              stickerFrame(now);
              window.dispatchEvent(new PointerEvent("pointermove", {
                clientX: st.cx, clientY: st.cy, bubbles: true,
              }));
            }
            for (let k = 0; k < 4; k++) {
              now += dt;
              im.update(dt, now);
              applyParams(outfitMap);
              stickerFrame(now);
            }
            app.renderer.render(app.stage);
            out.push(app.renderer.view.toDataURL("image/jpeg", o.quality || 0.8));
          }
          return out;
        },
        capture: (opts) => {
          const o = opts || {};
          const im = model.internalModel;
          const dt = Number(o.dt) || 33;
          let now = performance.now();
          const frames = Math.max(1, Number(o.frames) || 1);
          const mouse = o.mouse || null;
          for (let i = 0; i < frames; i++) {
            now += dt;
            im.update(dt, now);
            applyParams(outfitMap);
            stickerFrame(now);
            if (mouse) {
              const u = frames > 1 ? i / (frames - 1) : 0;
              const cx = mouse.cx0 + (mouse.cx1 - mouse.cx0) * u;
              const cy = mouse.cy0 + (mouse.cy1 - mouse.cy0) * u;
              window.dispatchEvent(new PointerEvent("pointermove", { clientX: cx, clientY: cy, bubbles: true }));
            }
          }
          app.renderer.render(app.stage); // refresh world transforms + framebuffer
          return app.renderer.view.toDataURL(o.type || "image/jpeg", o.quality || 0.8);
        },
        stickerFrame: () => { try { stickerFrame(performance.now()); const list = Object.keys(stickerSprites).map((k) => { const e = stickerSprites[k]; return { file: k.split('/').slice(-2).join('/'), target: e.target, alpha: Math.round(e.alpha * 100) / 100, texLoaded: e.sprite.texture.width > 2 }; }); return { ok: true, sprites: list }; } catch (err) { return { ok: false, err: String(err) }; } },
        stickers: () => stickerSprites,
        param: (id) => {
          try {
            return model.internalModel.coreModel.getParameterValueById(id);
          } catch (_) {
            return null;
          }
        },
      };
      setConn(connected ? "在线" : "模型已加载", connected);
    } catch (error) {
      console.error(error);
      showBubble("模型加载失败了…检查一下 pet-shell/assets/model。", { keep: true });
      setConn("模型加载失败", false);
    }
  }

  let reconnectDelay = 1000;

  function connect() {
    if (!WS_URL) {
      setConn("本地模式", true);
      return;
    }
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const url = `${WS_URL}?token=${encodeURIComponent(TOKEN)}`;
    ws = new WebSocket(url);
    ws.onopen = () => {
      connected = true;
      reconnectDelay = 1000; // success resets the backoff
      setConn("在线", true);
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      ws.send(JSON.stringify({ type: "hello", device: "pet-shell", version: 1 }));
    };
    ws.onclose = () => {
      connected = false;
      setConn("重连中…", false);
      ws = null;
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(30000, reconnectDelay * 1.7);
      }
    };
    ws.onerror = () => {
      setConn("连接失败", false);
    };
    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      const kind = msg.type || "";
      if (kind === "hello_ack") {
        setConn("在线", true);
        return;
      }
      if (kind === "state") {
        setConn(msg.state === "thinking" ? "在想…" : "在线", true);
        return;
      }
      if (kind === "bubble") {
        pendingId = "";
        send.disabled = false;
        const text = String(msg.text || "");
        showBubble(text);
        // Only an interactive reply from the pet surface is spoken; proactive
        // notifications would arrive on a separate type and stay silent.
        if (text) speak(text);
        return;
      }
      if (kind === "error") {
        send.disabled = false;
        if (msg.error === "cloud_offline") {
          showBubble("云端这会儿没连上…等一下再试试。");
        }
        return;
      }
    };
  }

  function submit() {
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    pendingId = `shell-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
    showBubble(text, { user: true, keep: true });
    input.value = "";
    send.disabled = true;
    ws.send(JSON.stringify({ type: "chat", id: pendingId, text }));
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  loadModel().then(connect);
})();
