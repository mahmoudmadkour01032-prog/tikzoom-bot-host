"""Arabic + English UI strings.

All templates use Telegram's HTML parse mode. Dynamic values that may
contain user-provided text MUST be HTML-escaped by callers via
``html.escape(value, quote=False)`` before being passed in. Plain
identifiers like integers or hex hashes do not need escaping.
"""
from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "ar": {
        "welcome_header": "🤖 <b>تيك زوم — منصة استضافة بوتات تلجرام</b>",
        "developed_by": '💎 المطوّر: <a href="https://t.me/MCV_M">@MCV_M</a>',
        "share_contact_required": (
            "📱 <b>لتشغيل البوت لازم تشارك جهة اتصالك أولاً.</b>\n\n"
            "اضغط الزر أسفل الكيبورد عشان نوثّق حسابك وتقدر ترفع بوتاتك."
        ),
        "share_contact_btn": "📱 مشاركة رقمي",
        "contact_saved": "✅ تم توثيق رقمك. أهلاً بيك على المنصة! 🎉",
        "force_sub_required": "🔔 <b>عشان تستخدم البوت لازم تشترك في القنوات دي:</b>",
        "force_sub_check_btn": "✅ تحقق من الاشتراك",
        "force_sub_subscribe_btn": "📢 اشتراك",
        "force_sub_ok": "✅ تم التحقق! استمتع بالخدمة. 🎉",
        "force_sub_fail": "❌ لازم تشترك في كل القنوات أولاً، ثم اضغط تحقق.",
        "main_menu_title": "🏠 <b>القائمة الرئيسية</b>",
        "btn_upload": "📤 رفع بوت جديد",
        "btn_my_bots": "🤖 بوتاتي",
        "btn_points": "💎 نقاطي وسرعتي",
        "btn_invite": "👥 دعوة الأصدقاء",
        "btn_admin": "⚙️ لوحة التحكم",
        "btn_developer": "💎 المطوّر",
        "btn_open_app": "🌐 فتح التطبيق",
        "btn_back": "🔙 رجوع",
        "btn_main": "🏠 الرئيسية",
        "btn_mcv": "🔴 MCV — المساعد الذكي",
        "btn_api": "🔴 API — خدمات للمطورين",
        "btn_api_docs": "📚 دليل الـ API الكامل",
        "btn_api_regenerate": "🔄 توليد مفتاح جديد",
        "btn_mcv_new_bot": "🤖 اعملي بوت جديد",
        "btn_mcv_chat": "💬 كلّم MCV",
        "btn_mcv_edit_bot": "✏️ عدّل بوت بـ MCV",
        "btn_mcv_convert": "🔄 حوّل لـ Python بـ MCV",
        "btn_change_token": "🔑 تغيير التوكن",
        "btn_run_file": "▶️ تشغيل الملف",
        "btn_save_only": "💾 تحميل بس",
        "btn_cancel": "❌ إلغاء",
        "points_header": "💎 <b>نقاطك ومستوى سرعتك</b>",
        "your_points": "نقاطك: <code>{points}</code>",
        "your_tier": "سرعتك الحالية: <b>{tier}</b>",
        "your_referrals": "عدد إحالاتك: <code>{count}</code>",
        "your_invite_link": "🔗 رابط دعوتك:\n<code>{link}</code>",
        "tier_unlocked": "✅ مفتوحة",
        "tier_locked": "🔒 مغلقة (محتاج {pts} نقطة)",
        "tier_vip_only": "⭐ VIP فقط",
        "tiers_table_header": "📊 <b>جدول السرعات</b>",
        "upload_choose_tier": "🎚 <b>اختر مستوى السرعة اللي عاوز ترفع عليه:</b>",
        "upload_send_file": (
            "📤 <b>ابعتلي ملف البوت دلوقتي:</b>\n"
            "نقبل: <code>.py</code> (Python) — <code>.php</code> (PHP) — "
            "<code>.js</code> (Node.js) — <code>.zip</code> / <code>.rar</code> (أرشيف)"
        ),
        "upload_no_capacity": (
            "❌ وصلت للحد الأقصى لعدد الملفات في سرعة {tier} ({limit} ملف).\n"
            "ادعُ أصدقاء أكتر أو ترقَّى لـ VIP لتفتح المزيد."
        ),
        "upload_invalid_type": (
            "❌ نوع الملف غير مدعوم. ابعت <code>.py</code> أو <code>.php</code> "
            "أو <code>.js</code> أو أرشيف."
        ),
        "upload_no_token": (
            "❌ ما عرفتش أستخرج توكن من الملف. تأكد إن التوكن مكتوب صراحة في الكود."
        ),
        "upload_invalid_token": (
            "❌ التوكن مش صحيح أو البوت متعلَّم عن طريق Telegram. جرّب توكن تاني."
        ),
        "upload_processing": "⏳ <b>بشغّل البوت بتاعك...</b> ثواني وأرجعلك بالنتيجة.",
        "upload_installing_deps": "📦 بنصّب المكتبات الناقصة... استنى ثواني.",
        "upload_success": (
            "✅ <b>تم رفع وتشغيل البوت بنجاح</b> 🎉\n\n"
            "📄 الملف: <code>{name}</code>\n"
            "🤖 البوت: @{bot_username}\n"
            "🚀 الحالة: <b>{status}</b>\n"
            "🔌 وضع التشغيل: <b>{mode}</b>\n"
            "🌐 ويب هوك: <code>{webhook_url}</code>"
        ),
        "my_bots_empty": "⚠️ ما رفعتش أي بوت لسه. اضغط <b>رفع بوت جديد</b> للبدء.",
        "my_bots_header": "🤖 <b>بوتاتي</b> (الصفحة {page}/{total})",
        "bot_running": "🟢 يعمل",
        "bot_stopped": "🔴 متوقف",
        "bot_crashed": "💥 توقف بسبب خطأ",
        "btn_run": "▶️ تشغيل",
        "btn_stop": "⏹️ إيقاف",
        "btn_restart": "🔄 إعادة تشغيل",
        "btn_delete": "🗑️ حذف",
        "btn_logs": "📜 السجل",
        "invite_text": (
            "👥 <b>نظام الإحالة</b>\n\n"
            "كل صديق يبدأ البوت من رابطك = +1 نقطة 💎\n"
            "اجمع نقاط لفتح سرعات أعلى واستضافة ملفات أكتر.\n\n"
            "🔗 رابطك:\n<code>{link}</code>\n\n"
            "📊 إحالاتك: <code>{count}</code> — نقاطك: <code>{points}</code>"
        ),
        "share_invite_btn": "📨 مشاركة الرابط",
        "referral_credited": "🎉 صديقك انضم بدعوتك! +1 نقطة 💎",
        "admin_only": "❌ هذا الأمر للأدمن فقط.",
        "banned": "🚫 تم حظرك من استخدام البوت.",
        "developer_panel": (
            "💎 <b>المطوّر</b>\n\n"
            'محمود — <a href="https://t.me/MCV_M">@MCV_M</a>'
        ),
    },
    "en": {
        "welcome_header": "🤖 <b>TikZoom — Telegram Bot Hosting Platform</b>",
        "developed_by": '💎 Developed by <a href="https://t.me/MCV_M">@MCV_M</a>',
        "share_contact_required": (
            "📱 <b>Please share your contact to use the bot.</b>\n\n"
            "Tap the button below the keyboard."
        ),
        "share_contact_btn": "📱 Share my contact",
        "contact_saved": "✅ Contact verified. Welcome aboard! 🎉",
        "force_sub_required": "🔔 <b>You must join these channels to use the bot:</b>",
        "force_sub_check_btn": "✅ Verify",
        "force_sub_subscribe_btn": "📢 Join",
        "force_sub_ok": "✅ Verified. Enjoy! 🎉",
        "force_sub_fail": "❌ You must join all channels first, then tap Verify.",
        "main_menu_title": "🏠 <b>Main Menu</b>",
        "btn_upload": "📤 Upload Bot",
        "btn_my_bots": "🤖 My Bots",
        "btn_points": "💎 Points & Tier",
        "btn_invite": "👥 Invite Friends",
        "btn_admin": "⚙️ Admin Panel",
        "btn_developer": "💎 Developer",
        "btn_open_app": "🌐 Open App",
        "btn_back": "🔙 Back",
        "btn_main": "🏠 Home",
        "btn_mcv": "🔴 MCV — AI assistant",
        "btn_api": "🔴 API — developer access",
        "btn_api_docs": "📚 Full API reference",
        "btn_api_regenerate": "🔄 Regenerate key",
        "btn_mcv_new_bot": "🤖 New bot",
        "btn_mcv_chat": "💬 Chat with MCV",
        "btn_mcv_edit_bot": "✏️ Edit bot with MCV",
        "btn_mcv_convert": "🔄 Convert to Python with MCV",
        "btn_change_token": "🔑 Change token",
        "btn_run_file": "▶️ Run file",
        "btn_save_only": "💾 Save only",
        "btn_cancel": "❌ Cancel",
        "points_header": "💎 <b>Your Points & Tier</b>",
        "your_points": "Points: <code>{points}</code>",
        "your_tier": "Current tier: <b>{tier}</b>",
        "your_referrals": "Referrals: <code>{count}</code>",
        "your_invite_link": "🔗 Invite link:\n<code>{link}</code>",
        "tier_unlocked": "✅ Unlocked",
        "tier_locked": "🔒 Locked (needs {pts} pts)",
        "tier_vip_only": "⭐ VIP only",
        "tiers_table_header": "📊 <b>Tiers</b>",
        "upload_choose_tier": "🎚 <b>Choose the speed tier:</b>",
        "upload_send_file": (
            "📤 <b>Send me your bot file:</b>\n"
            "Accepted: <code>.py</code> (Python) / <code>.php</code> / "
            "<code>.js</code> (Node.js) / archive"
        ),
        "upload_no_capacity": "❌ You reached the limit ({limit}) for tier {tier}.",
        "upload_invalid_type": "❌ Unsupported file type.",
        "upload_no_token": "❌ Could not extract a Telegram token from the file.",
        "upload_invalid_token": "❌ The token is invalid.",
        "upload_processing": "⏳ <b>Starting your bot...</b> hold on a moment.",
        "upload_installing_deps": "📦 Installing missing dependencies...",
        "upload_success": (
            "✅ <b>Bot uploaded and running</b> 🎉\n\n"
            "📄 File: <code>{name}</code>\n"
            "🤖 Bot: @{bot_username}\n"
            "🚀 Status: <b>{status}</b>\n"
            "🔌 Mode: <b>{mode}</b>\n"
            "🌐 Webhook: <code>{webhook_url}</code>"
        ),
        "my_bots_empty": "⚠️ No bots yet.",
        "my_bots_header": "🤖 <b>My Bots</b> (page {page}/{total})",
        "bot_running": "🟢 running",
        "bot_stopped": "🔴 stopped",
        "bot_crashed": "💥 crashed",
        "btn_run": "▶️ Start",
        "btn_stop": "⏹️ Stop",
        "btn_restart": "🔄 Restart",
        "btn_delete": "🗑️ Delete",
        "btn_logs": "📜 Logs",
        "invite_text": (
            "👥 <b>Referrals</b>\n\n+1 point per friend who starts the bot.\n\n"
            "🔗 <code>{link}</code>\n\n"
            "Referrals: <code>{count}</code> — Points: <code>{points}</code>"
        ),
        "share_invite_btn": "📨 Share link",
        "referral_credited": "🎉 New referral! +1 point 💎",
        "admin_only": "❌ Admin only.",
        "banned": "🚫 You are banned.",
        "developer_panel": (
            "💎 <b>Developer</b>\n\n"
            'Mahmoud — <a href="https://t.me/MCV_M">@MCV_M</a>'
        ),
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    table = STRINGS.get(lang) or STRINGS["ar"]
    text = table.get(key) or STRINGS["ar"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError:
            return text
    return text
