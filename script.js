const startBtn = document.getElementById('startBtn');
const breathText = document.getElementById('breathText');
const timerDisplay = document.getElementById('timerDisplay');
const cycleCounter = document.getElementById('cycleCounter');
const circle = document.querySelector('.circle');

let currentCycle = 0;
const totalCycles = 15;
let currentInterval = null;

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
}