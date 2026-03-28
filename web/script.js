const socket = io();
let partsList = {};

// ── 演出：Matrixノイズ ──
function triggerMatrix() {
    const canvas = document.getElementById('matrix-canvas');
    const ctx = canvas.getContext('2d');
    const overlay = document.getElementById('matrix-overlay');
    
    overlay.style.display = 'block';
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;

    const alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ@#$';
    const fontSize = 16;
    const columns = canvas.width / fontSize;
    const rainDrops = Array(Math.floor(columns)).fill(1);

    const draw = () => {
        ctx.fillStyle = 'rgba(0, 0, 0, 0.05)';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#00e5ff';
        ctx.font = fontSize + 'px monospace';
        for (let i = 0; i < rainDrops.length; i++) {
            const text = alphabet[Math.floor(Math.random() * alphabet.length)];
            ctx.fillText(text, i * fontSize, rainDrops[i] * fontSize);
            if (rainDrops[i] * fontSize > canvas.height && Math.random() > 0.975) rainDrops[i] = 0;
            rainDrops[i]++;
        }
    };
    const timer = setInterval(draw, 30);
    setTimeout(() => { 
        clearInterval(timer); 
        overlay.style.opacity = '0';
        setTimeout(() => { overlay.style.display = 'none'; overlay.style.opacity = '1'; }, 1000);
    }, 2500);
}

// ── キューバナー操作 ──
function showQueueBanner(position, total) {
    const banner  = document.getElementById('queue-banner');
    const msgEl   = document.getElementById('queue-message');
    const barEl   = document.getElementById('queue-progress-bar');

    msgEl.textContent = `あと ${position} 人で順番です（現在 ${total} 人待ち）`;

    // position=total のとき 0%、position=1 のとき 90% に近づく
    const pct = total > 1 ? Math.round(((total - position) / (total - 1)) * 90) : 0;
    barEl.style.width = pct + '%';

    banner.classList.add('visible');
}

function hideQueueBanner() {
    const banner = document.getElementById('queue-banner');
    banner.classList.remove('visible');
}

// ── 受信イベント ──
socket.on("admin_auth_success", (args) => {
    const data = Array.isArray(args) ? args[0] : args;
    triggerMatrix();
    
    document.getElementById('auth-flash').style.display = 'block';
    setTimeout(() => document.getElementById('auth-flash').style.display = 'none', 400);

    const b = document.getElementById('mode-badge');
    b.innerText = "ADMIN MODE / UNRESTRICTED";
    b.className = 'admin';

    const p = document.getElementById("debug-panel");
    if(p) p.style.display = "block";
    document.getElementById("admin-status").innerText = data.message || "AUTHORIZED";
    document.getElementById("stop-btn").disabled = false;
});

socket.on("update_status", (args) => {
    const [text] = args;
    document.getElementById('status-display').innerText = text;
    const dot = document.getElementById('status-dot');
    text.includes("READY") ? dot.classList.remove('active') : dot.classList.add('active');
    
    if(text.includes("STEP")) {
        const m = text.match(/\d+/);
        if(m) document.getElementById('loop-count').innerText = m[0];
    }
});

socket.on("add_log", (args) => {
    const [msg, level] = args;
    const win = document.getElementById('log-window');
    const div = document.createElement('div');
    div.className = `log-entry ${level}`;
    div.innerHTML = `<span style="opacity:0.3">[${new Date().toLocaleTimeString()}]</span> ${msg}`;
    win.appendChild(div);
    win.scrollTop = win.scrollHeight;
});

socket.on("add_reveal_card", (args) => {
    const [cat, name, price] = args;
    partsList[cat] = parseInt(price);
    updateTotal();
    
    const container = document.getElementById('cards-container');
    const empty = container.querySelector('.cards-empty');
    if(empty) empty.remove();

    let card = document.getElementById(`card-${cat}`);
    if(!card) {
        card = document.createElement('div');
        card.id = `card-${cat}`; 
        card.className = 'part-card';
        container.appendChild(card);
    }
    card.innerHTML = `
        <div style="font-size:10px;color:var(--text-sec);text-transform:uppercase;">${cat}</div>
        <div style="font-size:14px;font-weight:600;margin:6px 0;line-height:1.2;">${name}</div>
        <div style="text-align:right;color:var(--accent);font-family:var(--font-mono);font-size:16px;">
            ¥${Number(price).toLocaleString()}
        </div>
    `;
});

// ── キューイベント受信 ──
socket.on("queue_waiting", (args) => {
    const data = Array.isArray(args) ? args[0] : args;
    showQueueBanner(data.position, data.total);
});

socket.on("queue_position_update", (args) => {
    const data = Array.isArray(args) ? args[0] : args;
    showQueueBanner(data.position, data.total);
});

socket.on("queue_started", (args) => {
    const data = Array.isArray(args) ? args[0] : args;
    hideQueueBanner();
    // 「待機中だったので start-btn はまだ disabled」→ここで再起動状態に戻す
    const btn = document.getElementById('start-btn');
    btn.innerHTML = '<i class="fas fa-cog fa-spin"></i> EXECUTING...';
    // ログにも表示
    const win = document.getElementById('log-window');
    const div = document.createElement('div');
    div.className = 'log-entry system';
    div.innerHTML = `<span style="opacity:0.3">[${new Date().toLocaleTimeString()}]</span> ${data.message}`;
    win.appendChild(div);
    win.scrollTop = win.scrollHeight;
});

// ── 送信関数 ──
function startBuild() {
    const btn = document.getElementById('start-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-cog fa-spin"></i> EXECUTING...';
    
    document.getElementById('cards-container').innerHTML = '';
    document.getElementById('log-window').innerHTML = '<div class="log-entry system">Initializing engine...</div>';
    partsList = {}; 
    updateTotal();

    socket.emit("start_build_sequence", {
        budget: document.getElementById('budget').value,
        purpose: document.getElementById('purpose').value
    });
}

function sendInstruction() {
    const input = document.getElementById('cmd-input');
    const text = input.value.trim();
    if(!text) return;
    socket.emit("receive_user_instruction", { text });
    input.value = '';
}

function triggerEmergencyStop() {
    if (confirm("【警告】全演算プロセスを強制停止しますか？")) {
        socket.emit("receive_user_instruction", { text: "STOP" });
    }
}

function updateTotal() {
    const total = Object.values(partsList).reduce((a, b) => a + b, 0);
    document.getElementById('current-total').innerText = `¥${total.toLocaleString()}`;
}

// Enterキー入力
document.getElementById('cmd-input').addEventListener('keypress', (e) => {
    if(e.key === 'Enter') sendInstruction();
});