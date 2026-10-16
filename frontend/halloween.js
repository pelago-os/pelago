// 万圣节装饰：背景里慢慢漂的磷光水母 + 三个角落：右下一盏南瓜灯、左上小幽灵、右上小蝙蝠（遮挡最小的排法）。
// 只在 data-theme="halloween" 时才显示（样式在 halloween.css 里控制），其他主题里这层是 display:none。
// 只往页面里加一个装饰容器，不读不写任何数据，不改已有元素。
(function () {
  if (document.getElementById('halloween-decor')) return;

  // 一个南瓜灯：橙色瓜身（三瓣）、绿茎、三角眼 + 锯齿嘴，里面一团会闪的烛火。
  // 每盏用不同 id 前缀，避免 SVG 渐变 id 冲突。
  function lamp(id, cls) {
    return `
    <div class="hw-lamp ${cls}" aria-hidden="true">
      <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <radialGradient id="${id}-body" cx="50%" cy="45%" r="60%">
            <stop offset="0%" stop-color="#ffb347"/>
            <stop offset="60%" stop-color="#ff8a1f"/>
            <stop offset="100%" stop-color="#c24a00"/>
          </radialGradient>
          <radialGradient id="${id}-fire" cx="50%" cy="60%" r="55%">
            <stop offset="0%" stop-color="#fff6c2"/>
            <stop offset="55%" stop-color="#ffd23f"/>
            <stop offset="100%" stop-color="#ff8a1f" stop-opacity="0"/>
          </radialGradient>
        </defs>
        <!-- 茎 -->
        <path d="M46 18 C46 10 52 8 56 6 C56 12 54 16 54 22 Z" fill="#4e7a2a"/>
        <path d="M50 22 C56 14 66 14 70 18" stroke="#5f9a33" stroke-width="2.4" fill="none" stroke-linecap="round"/>
        <!-- 瓜身：三瓣 -->
        <ellipse cx="29" cy="58" rx="22" ry="32" fill="url(#${id}-body)"/>
        <ellipse cx="71" cy="58" rx="22" ry="32" fill="url(#${id}-body)"/>
        <ellipse cx="50" cy="58" rx="24" ry="36" fill="url(#${id}-body)"/>
        <path d="M50 24 C46 40 46 76 50 92" stroke="#b34400" stroke-width="1.2" fill="none" opacity=".55"/>
        <path d="M33 28 C27 44 27 72 33 88" stroke="#b34400" stroke-width="1" fill="none" opacity=".4"/>
        <path d="M67 28 C73 44 73 72 67 88" stroke="#b34400" stroke-width="1" fill="none" opacity=".4"/>
        <!-- 烛光从脸里透出来 -->
        <ellipse class="hw-flame" cx="50" cy="62" rx="20" ry="22" fill="url(#${id}-fire)"/>
        <!-- 眼睛和嘴：深色镂空，上面再叠一点亮色 -->
        <path d="M30 48 L42 44 L38 58 Z" fill="#2a1030"/>
        <path d="M70 48 L58 44 L62 58 Z" fill="#2a1030"/>
        <path d="M50 60 L46 68 L54 68 Z" fill="#2a1030"/>
        <path d="M28 72 Q50 94 72 72 L66 74 L62 80 L56 74 L50 82 L44 74 L38 80 L34 74 Z" fill="#2a1030"/>
        <path class="hw-flame" d="M33 49 L41 47 L39 55 Z M67 49 L59 47 L61 55 Z M50 62 L48 66 L52 66 Z" fill="#ffd23f" opacity=".9"/>
      </svg>
    </div>`;
  }


  // 一只磷光水母：半透明的伞盖加几条会摆动的触须。颜色用主题里的磷光绿，发一点光。
  // 每只用不同 id 前缀，避免 SVG 渐变 id 冲突。
  function jelly(id, cls) {
    return `
    <div class="hw-jelly ${cls}" aria-hidden="true">
      <svg viewBox="0 0 80 120" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <radialGradient id="${id}-bell" cx="50%" cy="60%" r="62%">
            <stop offset="0%" stop-color="#eafff3" stop-opacity=".85"/>
            <stop offset="45%" stop-color="#7dffb0" stop-opacity=".55"/>
            <stop offset="100%" stop-color="#7dffb0" stop-opacity=".10"/>
          </radialGradient>
        </defs>
        <g class="hw-jelly-tentacles" stroke="#7dffb0" stroke-linecap="round" fill="none">
          <path d="M22 52 C16 70 28 82 20 104" stroke-width="2.2" opacity=".55"/>
          <path d="M33 54 C29 74 40 88 32 114" stroke-width="2" opacity=".5"/>
          <path d="M47 54 C51 74 40 88 48 114" stroke-width="2" opacity=".5"/>
          <path d="M58 52 C64 70 52 82 60 104" stroke-width="2.2" opacity=".55"/>
          <path d="M40 54 C38 70 44 80 40 96" stroke-width="3" opacity=".35"/>
        </g>
        <path class="hw-jelly-bell" d="M8 54 C8 24 24 8 40 8 C56 8 72 24 72 54 C64 48 56 54 48 49 C44 54 36 54 32 49 C24 54 16 48 8 54 Z" fill="url(#${id}-bell)" stroke="#bfffe0" stroke-opacity=".6" stroke-width="1.2"/>
        <ellipse cx="40" cy="30" rx="13" ry="7" fill="#ffffff" opacity=".28"/>
      </svg>
    </div>`;
  }

  // 小角色：直接用 emoji，手机上是系统自带的彩色小图。位置、大小、飘动节奏各不相同，别排得太整齐。
  function critter(emoji, cls, label) {
    return `<div class="hw-critter ${cls}" aria-hidden="true" title="${label}">${emoji}</div>`;
  }

  const wrap = document.createElement('div');
  wrap.id = 'halloween-decor';
  wrap.className = 'halloween-decor';
  wrap.innerHTML =
    jelly('hwj1', 'hw-j1') + jelly('hwj2', 'hw-j2') + jelly('hwj3', 'hw-j3') +   // 背景里慢慢漂的三只磷光水母
    critter('👻', 'hw-ghost', '小幽灵') +                    // 左上角：小幽灵，趴在 logo 旁边探头
    critter('🦇', 'hw-bat', '小蝙蝠') +                      // 右上角：小蝙蝠
    lamp('hwa', 'hw-br');                                   // 右下角：一盏南瓜灯（会闪的烛火）
  document.body.appendChild(wrap);

  // ── 点南瓜灯：噗地喷出一把糖果（彩蛋）──────────────────────────
  // 只有南瓜灯本身能被点（其他装饰仍然点击穿透）；按钮里只有"送糖"（点赞）会冒糖果，见 hwCandyBurst。
  // 克制：每次最多 10 颗；有冷却（700ms），连点不会堆积；糖果飘一会儿就消失并从页面移除。
  // 有意的例外：系统开着"减少动态效果"时，水母、烛火等持续动画仍然静止，
  // 但点南瓜喷糖照常工作。理由：它是用户主动点一下才触发、只持续约 1.5 秒、有冷却的小动作，
  // 不是会让人头晕的持续背景动画。
  const CANDY = ['🍬', '🍭', '🍫', '🎃', '🍬', '🍭'];
  let burst = 0;

  // 从某个元素里喷一把糖。angleFrom/angleSpan 是喷射扇形（弧度，0 是正右方，往上为正）。
  function candyBurst(el, count, angleFrom, angleSpan, minDist, rangeDist) {
    if (!el || document.documentElement.getAttribute('data-theme') !== 'halloween') return;
    const box = el.getBoundingClientRect();
    const ox = box.left + box.width / 2, oy = box.top + box.height * 0.45;
    burst += 1;
    for (let i = 0; i < count; i += 1) {
      const piece = document.createElement('span');
      piece.className = 'hw-candy';
      piece.textContent = CANDY[(burst + i) % CANDY.length];
      // 每颗的方向、距离、转角、时长都不同
      const angle = Math.PI * (angleFrom + Math.random() * angleSpan);
      const dist = minDist + Math.random() * rangeDist;
      piece.style.left = ox + 'px';
      piece.style.top = oy + 'px';
      piece.style.setProperty('--dx', Math.round(Math.cos(angle) * dist) + 'px');
      piece.style.setProperty('--dy', Math.round(-Math.abs(Math.sin(angle)) * dist - 30) + 'px');
      piece.style.setProperty('--rot', Math.round((Math.random() - 0.5) * 540) + 'deg');
      const seconds = 0.9 + Math.random() * 0.7;
      piece.style.animationDuration = seconds + 's';
      piece.style.fontSize = (18 + Math.round(Math.random() * 10)) + 'px';
      // 动画结束就移除；万一 animationend 没触发（页面在后台、动画被系统关掉等），再用定时器兜底强制移除，
      // 保证糖果绝不会越积越多。
      piece.addEventListener('animationend', () => piece.remove(), { once: true });
      setTimeout(() => piece.remove(), Math.ceil(seconds * 1000) + 600);
      wrap.appendChild(piece);
    }
  }

  // 送糖（万圣节主题里的点赞）：从按钮往上蹦三颗。和南瓜灯一样是用户主动点一下才有的小动作，
  // 同样不受"减少动态效果"限制；冷却 700ms，连点不会堆积。
  let likeCooling = false;
  window.hwCandyBurst = (el) => {
    if (likeCooling) return;
    likeCooling = true;
    setTimeout(() => { likeCooling = false; }, 700);
    candyBurst(el, 3, 0.25, 0.5, 40, 50);
  };

  const lampEl = wrap.querySelector('.hw-lamp');
  let cooling = false;
  if (lampEl) {
    lampEl.setAttribute('role', 'button');
    lampEl.setAttribute('aria-label', '点一下南瓜灯，会掉出糖果');
    lampEl.addEventListener('click', () => {
      if (cooling || document.documentElement.getAttribute('data-theme') !== 'halloween') return;
      cooling = true;
      setTimeout(() => { cooling = false; }, 700);
      lampEl.classList.remove('hw-lamp-pop'); void lampEl.offsetWidth; lampEl.classList.add('hw-lamp-pop');
      // 朝左上扇形喷出（南瓜在右下角，往屏幕里面喷）
      candyBurst(lampEl, 10, 0.55, 0.9, 70, 130);
    });
  }
})();
