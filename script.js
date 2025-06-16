const startBtn = document.getElementById('startBtn');
const breathText = document.getElementById('breathText');
const timerDisplay = document.getElementById('timerDisplay');
const cycleCounter = document.getElementById('cycleCounter');
const circle = document.querySelector('.circle');
const visitCount = document.getElementById('visitCount');
const dailyCount = document.getElementById('dailyCount');
const weeklyCount = document.getElementById('weeklyCount');
const monthlyCount = document.getElementById('monthlyCount');
const motivationText = document.getElementById('motivationText');

let currentCycle = 0;
const totalCycles = 15;
let currentInterval = null;

const motivationalMessages = [
    'واصل الاستمرار! أنت على الطريق الصحيح!',
    'كل نفس يقربك من الراحة!',
    'أنت قوي، وهذا التمرين يعزز شفاءك!',
    'اليوم خطوة جديدة نحو التحسن!'
];

// Initialize localStorage
let stats = JSON.parse(localStorage.getItem('breathingStats')) || {
    visits: 0,
    daily: { count: 0, lastReset: new Date().toISOString().split('T')[0] },
    weekly: [],
    monthly: []
};

// Increment visits and update stats
stats.visits++;
if (stats.daily.lastReset !== new Date().toISOString().split('T')[0]) {
    stats.daily.count = 0;
    stats.daily.lastReset = new Date().toISOString().split('T')[0];
}
localStorage.setItem('breathingStats', JSON.stringify(stats));
updateStatsDisplay();

// Chart.js configuration
const ctx = document.getElementById('progressChart').getContext('2d');
const progressChart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
            label: 'التمارين الأسبوعية',
            data: [],
            borderColor: '#2563eb',
            backgroundColor: 'rgba(37, 99, 235, 0.2)',
            fill: true,
            tension: 0.4
        }]
    },
    options: {
        responsive: true,
        scales: {
            y: { beginAtZero: true, title: { display: true, text: 'عدد التمارين' } },
            x: { title: { display: true, text: 'الأيام' } }
        },
        plugins: { legend: { display: false } }
    }
});

updateChart();

startBtn.addEventListener('click', startExercise);

function startExercise() {
    startBtn.disabled = true;
    currentCycle = 0;
    updateCycleCounter();
    nextCycle();
}

function nextCycle() {
    if (currentCycle < totalCycles) {
        currentCycle++;
        updateCycleCounter();
        runBreathingCycle();
    } else {
        endExercise();
    }
}

function updateCycleCounter() {
    cycleCounter.textContent = `الدورة: ${currentCycle}/${totalCycles}`;
}

function runBreathingCycle() {
    breathe('استنشق', 4, 'inhale')
        .then(() => breathe('اكتم', 4, 'hold'))
        .then(() => breathe('ازفر', 6, 'exhale'))
        .then(() => nextCycle());
}

function breathe(text, duration, animation) {
    return new Promise(resolve => {
        breathText.textContent = text;
        timerDisplay.textContent = duration;
        circle.className = `circle ${animation}`;

        currentInterval = setInterval(() => {
            duration--;
            timerDisplay.textContent = duration;
            if (duration <= 0) {
                clearInterval(currentInterval);
                resolve();
            }
        }, 1000);
    });
}

function endExercise() {
    startBtn.disabled = false;
    breathText.textContent = 'أكملت التمرين! اضغط "ابدأ" للتكرار';
    timerDisplay.textContent = '0';
    circle.className = 'circle';
    currentInterval = null;

    // Update stats
    stats.daily.count++;
    const today = new Date().toISOString().split('T')[0];
    stats.weekly = stats.weekly.filter(d => new Date(d.date) > new Date(Date.now() - 7 * 24 * 60 * 60 * 1000));
    stats.monthly = stats.monthly.filter(d => new Date(d.date) > new Date(Date.now() - 30 * 24 * 60 * 60 * 1000));
    stats.weekly.push({ date: today, count: 1 });
    stats.monthly.push({ date: today, count: 1 });
    localStorage.setItem('breathingStats', JSON.stringify(stats));
    updateStatsDisplay();
    updateChart();
}

function updateStatsDisplay() {
    visitCount.textContent = stats.visits;
    dailyCount.textContent = stats.daily.count;
    weeklyCount.textContent = stats.weekly.reduce((sum, d) => sum + d.count, 0);
    monthlyCount.textContent = stats.monthly.reduce((sum, d) => sum + d.count, 0);
    motivationText.textContent = motivationalMessages[Math.floor(Math.random() * motivationalMessages.length)];
}

function updateChart() {
    const last7Days = Array.from({ length: 7 }, (_, i) => {
        const date = new Date(Date.now() - i * 24 * 60 * 60 * 1000);
        return date.toISOString().split('T')[0];
    }).reverse();

    const weeklyData = last7Days.map(day => {
        const dayData = stats.weekly.find(d => d.date === day);
        return dayData ? dayData.count : 0;
    });

    progressChart.data.labels = last7Days.map(day => new Date(day).toLocaleDateString('ar-SA', { weekday: 'short' }));
    progressChart.data.datasets[0].data = weeklyData;
    progressChart.update();
}