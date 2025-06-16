// طلب إذن الإشعارات
if (Notification.permission !== "granted") {
    Notification.requestPermission();
}

// قائمة الآيات القرآنية مع روابط التلاوة
const quranVerses = [
    {
        text: "وَإِذَا سَأَلَكَ عِبَادِي عَنِّي فَإِنِّي قَرِيبٌ ۖ أُجِيبُ دَعْوَةَ الدَّاعِ إِذَا دَعَانِ ۖ فَلْيَسْتَجِيبُوا لِي وَلْيُؤْمِنُوا بِي لَعَلَّهُمْ يَرْشُدُونَ (البقرة: 186)",
        audio: "https://everyayah.com/data/Abdul_Basit_Murattal_64kbps/002186.mp3"
    },
    {
        text: "الَّذِينَ آمَنُوا وَتَطْمَئِنُّ قُلُوبُهُم بِذِكْرِ اللَّهِ ۗ أَلَا بِذِكْرِ اللَّهِ تَطْمَئِنُّ الْقُلُوبُ (الرعد: 28)",
        audio: "https://everyayah.com/data/Abdul_Basit_Murattal_64kbps/013028.mp3"
    },
    {
        text: "قُلِ اللَّهُمَّ مَالِكَ الْمُلْكِ تُؤْتِي الْمُلْكَ مَن تَشَاءُ وَتَنزِعُ الْمُلْكَ مِمَّن تَشَاءُ وَتُعِزُّ مَن تَشَاءُ وَتُذِلُّ مَن تَشَاءُ ۖ بِيَدِكَ الْخَيْرُ ۖ إِنَّكَ عَلَىٰ كُلِّ شَيْءٍ قَدِيرٌ (آل عمران: 26)",
        audio: "https://everyayah.com/data/Abdul_Basit_Murattal_64kbps/003026.mp3"
    },
    {
        text: "إِنَّ الَّذِينَ آمَنُوا وَعَمِلُوا الصَّالِحَاتِ سَيَجْعَلُ لَهُمُ الرَّحْمَٰنُ وُدًّا (مريم: 96)",
        audio: "https://everyayah.com/data/Abdul_Basit_Murattal_64kbps/019096.mp3"
    }
];

// متغيرات
let isBreathing = false;
let currentPhase = "";
let reps = 0;
let sessionsToday = 0;
let weeklySessions = 0;
let monthlySessions = 0;
let totalReps = 0;
let points = 0;
let badges = [];
let userName = "";
let notificationTimes = [];
const maxSessionsPerDay = 3;
const maxRepsPerSession = 15;
const motivationMessages = [
    "رائع! أنت تقترب من الهدوء! 🌟",
    "استمر، أنت تتحكم بتوترك! 💪",
    "تنفس بعمق، أنت نجم! ✨",
    "كل نفس يقربك من السكينة! 🧘",
    "أحسنت! أنت تهزم التوتر! 🏆"
];

// تحميل بيانات المستخدم
function loadUserData() {
    const savedData = JSON.parse(localStorage.getItem("breathingAppData")) || {};
    console.log("Loading user data:", savedData); // تصحيح أخطاء
    if (savedData.userName) {
        userName = savedData.userName;
        notificationTimes = savedData.notificationTimes || [];
        sessionsToday = savedData.stats?.today?.sessions || 0;
        weeklySessions = savedData.stats?.week?.sessions || 0;
        monthlySessions = savedData.stats?.month?.sessions || 0;
        totalReps = savedData.stats?.totalReps || 0;
        points = savedData.stats?.points || 0;
        badges = savedData.stats?.badges || [];
        document.getElementById("loginContainer").style.display = "none";
        document.getElementById("mainContainer").style.display = "block";
        document.getElementById("userGreeting").textContent = userName;
        updateStatsDisplay();
        updateNotificationList();
        scheduleNotifications();
        applySettings();
        displayRandomVerse();
    } else {
        document.getElementById("loginContainer").style.display = "block";
        document.getElementById("mainContainer").style.display = "none";
    }
}

// عرض آية عشوائية
function displayRandomVerse() {
    const randomIndex = Math.floor(Math.random() * quranVerses.length);
    const verse = quranVerses[randomIndex];
    document.getElementById("verseText").textContent = verse.text;
    document.getElementById("verseAudio").src = verse.audio;
}

// حفظ بيانات المستخدم
function saveUser() {
    userName = document.getElementById("userName").value.trim();
    notificationTimes = Array.from(document.querySelectorAll(".notification-time"))
        .map(input => input.value)
        .filter(time => time);
    
    console.log("Saving user:", userName, notificationTimes); // تصحيح أخطاء
    if (!userName) {
        alert("يرجى إدخال اسمك!");
        return;
    }
    if (notificationTimes.length < 3) {
        alert("يرجى إدخال 3 مواعيد تنبيه على الأقل!");
        return;
    }

    const data = {
        userName,
        notificationTimes,
        stats: {
            today: { sessions: 0, date: new Date().toDateString() },
            week: { sessions: 0, weekNumber: getWeekNumber() },
            month: { sessions: 0, month: new Date().getMonth() },
            totalReps: 0,
            points: 0,
            badges: []
        }
    };
    localStorage.setItem("breathingAppData", JSON.stringify(data));
    console.log("Data saved to localStorage:", data); // تصحيح أخطاء
    loadUserData();
}

// إضافة موعد تنبيه
function addNotificationTime() {
    const input = document.createElement("input");
    input.type = "time";
    input.className = "notification-time";
    document.getElementById("notificationTimes").appendChild(input);
}

// تحديث قائمة التنبيهات
function updateNotificationList() {
    const list = document.getElementById("notificationList");
    list.innerHTML = "";
    notificationTimes.forEach((time, index) => {
        const div = document.createElement("div");
        div.className = "notification-list-item";
        div.innerHTML = `
            <span>${time}</span>
            <button onclick="removeNotification(${index})">حذف</button>
        `;
        list.appendChild(div);
    });
}

// إضافة تنبيه جديد
function addNewNotification() {
    const newTime = document.getElementById("newNotificationTime").value;
    if (newTime && !notificationTimes.includes(newTime)) {
        notificationTimes.push(newTime);
        saveUserData();
        updateNotificationList();
        scheduleNotifications();
    }
}

// حذف تنبيه
function removeNotification(index) {
    notificationTimes.splice(index, 1);
    saveUserData();
    updateNotificationList();
    scheduleNotifications();
}

// حفظ بيانات الإحصائيات
function saveUserData() {
    const data = {
        userName,
        notificationTimes,
        stats: {
            today: { sessions: sessionsToday, date: new Date().toDateString() },
            week: { sessions: weeklySessions, weekNumber: getWeekNumber() },
            month: { sessions: monthlySessions, month: new Date().getMonth() },
            totalReps,
            points,
            badges
        }
    };
    localStorage.setItem("breathingAppData", JSON.stringify(data));
}

// تحديث واجهة الإحصائيات
function updateStatsDisplay() {
    document.getElementById("dailyCount").textContent = sessionsToday;
    document.getElementById("weeklyCount").textContent = weeklySessions;
    document.getElementById("monthlyCount").textContent = monthlySessions;
    document.getElementById("points").textContent = points;
    document.getElementById("totalReps").textContent = totalReps;
    document.getElementById("badges").textContent = badges.length ? badges.join(", ") : "لا توجد شارات بعد";

    // تحديث الحالة المزاجية
    const motivationText = document.getElementById("motivationText");
    const motivationImage = document.getElementById("motivationImage");
    if (sessionsToday === 0) {
        motivationText.textContent = `يا ${userName}! لم تتمرن اليوم! هيا، ابدأ الآن! 😔`;
        motivationImage.src = "https://cdn.pixabay.com/photo/2016/08/08/09/17/avatar-1577909_1280.png"; // وجه حزين
    } else if (sessionsToday < 3) {
        motivationText.textContent = `جيد يا ${userName}! حاول إكمال الجلسات الثلاث! 😊`;
        motivationImage.src = "https://cdn.pixabay.com/photo/2016/08/08/09/17/avatar-1577909_1280.png"; // وجه محايد
    } else {
        motivationText.textContent = motivationMessages[Math.floor(Math.random() * motivationMessages.length)];
        motivationImage.src = "https://cdn.pixabay.com/photo/2016/08/08/09/17/avatar-1577909_1280.png"; // وجه سعيد
    }
}

// حساب رقم الأسبوع
function getWeekNumber() {
    const now = new Date();
    const start = new Date(now.getFullYear(), 0, 1);
    return Math.ceil(((now - start) / 86400000 + start.getDay() + 1) / 7);
}

// منطق التنفس
function startBreathing() {
    if (sessionsToday >= maxSessionsPerDay) {
        alert(`يا ${userName}! لقد أكملت الجلسات الثلاث لهذا اليوم! 🎉`);
        return;
    }

    isBreathing = true;
    reps = 0;
    document.getElementById("startBtn").disabled = true;
    document.getElementById("pauseBtn").disabled = false;
    document.getElementById("resumeBtn").disabled = true;
    runBreathingCycle();
}

function runBreathingCycle() {
    if (!isBreathing || reps >= maxRepsPerSession) {
        if (reps >= maxRepsPerSession) {
            sessionsToday++;
            weeklySessions++;
            monthlySessions++;
            totalReps += maxRepsPerSession;
            points += 10;
            checkBadges();
            saveUserData();
            updateStatsDisplay();
            alert(`أكملت الجلسة يا ${userName}! 🎉 حصلت على 10 نقاط!`);
            resetBreathing();
        }
        return;
    }

    const circle = document.getElementById("breathingCircle");
    const circleText = document.getElementById("circleText");
    const soundToggle = document.getElementById("soundToggle").checked;

    // شهيق
    currentPhase = "inhale";
    circleText.textContent = "شهيق";
    circle.style.background = "#74ebd5";
    circle.style.transform = "scale(1.2)";
    if (soundToggle) document.getElementById("inhaleSound").play();
    setTimeout(() => {
        if (currentPhase !== "inhale") return;
        // حبس
        currentPhase = "hold";
        circleText.textContent = "حبس";
        circle.style.background = "#acb6e5";
        setTimeout(() => {
            if (currentPhase !== "hold") return;
            // زفير
            currentPhase = "exhale";
            circleText.textContent = "زفير";
            circle.style.background = "#ff6f61";
            circle.style.transform = "scale(1)";
            if (soundToggle) document.getElementById("exhaleSound").play();
            setTimeout(() => {
                reps++;
                totalReps++;
                saveUserData();
                updateStatsDisplay();
                runBreathingCycle();
            }, 6000);
        }, 4000);
    }, 4000);
}

function pauseBreathing() {
    isBreathing = false;
    document.getElementById("pauseBtn").disabled = true;
    document.getElementById("resumeBtn").disabled = false;
    document.getElementById("circleText").textContent = "متوقف";
}

function resumeBreathing() {
    isBreathing = true;
    document.getElementById("pauseBtn").disabled = false;
    document.getElementById("resumeBtn").disabled = true;
    runBreathingCycle();
}

function resetBreathing() {
    isBreathing = false;
    document.getElementById("startBtn").disabled = false;
    document.getElementById("pauseBtn").disabled = true;
    document.getElementById("resumeBtn").disabled = true;
    document.getElementById("circleText").textContent = "ابدأ التمرين";
    document.getElementById("breathingCircle").style.background = "#74ebd5";
}

// التحقق من الشارات
function checkBadges() {
    if (totalReps >= 150 && !badges.includes("بطل التنفس")) {
        badges.push("بطل التنفس");
        alert("مبروك! حصلت على شارة 'بطل التنفس' 🏅");
    }
    if (weeklySessions >= 10 && !badges.includes("محترف الأسبوع")) {
        badges.push("محترف الأسبوع");
        alert("مبروك! حصلت على شارة 'محترف الأسبوع' 🏆");
    }
    saveUserData();
}

// إعداد التنبيهات
function scheduleNotifications() {
    notificationTimes.forEach(time => {
        const [hour, minute] = time.split(":").map(Number);
        const now = new Date();
        let notificationTime = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, minute);
        if (notificationTime < now) {
            notificationTime.setDate(now.getDate() + 1);
        }
        const timeToNotification = notificationTime - now;
        console.log(`Scheduling notification for ${time} in ${timeToNotification}ms`); // تصحيح أخطاء
        setTimeout(() => {
            if (Notification.permission === "granted") {
                new Notification(`حان وقت تمرين التنفس يا ${userName}! 🧘`, {
                    body: "ابدأ جلسة التنفس الآن لتقليل التوتر!"
                });
            }
            scheduleNotifications();
        }, timeToNotification);
    });
}

// تطبيق إعدادات التخصيص
function applySettings() {
    const bgColor = document.getElementById("bgColor").value;
    document.body.style.background = `linear-gradient(135deg, ${bgColor}, #acb6e5)`;
    document.getElementById("bgColor").addEventListener("change", () => {
        document.body.style.background = `linear-gradient(135deg, ${document.getElementById("bgColor").value}, #acb6e5)`;
    });
}

// إعداد الأزرار
document.getElementById("startBtn").addEventListener("click", startBreathing);
document.getElementById("pauseBtn").addEventListener("click", pauseBreathing);
document.getElementById("resumeBtn").addEventListener("click", resumeBreathing);

// ربط زر "حفظ والبدء"
document.querySelector(".login-container button[onclick='saveUser()']").addEventListener("click", saveUser);

// تحميل البيانات
loadUserData();