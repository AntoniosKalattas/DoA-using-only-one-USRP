package com.antonioskalattas.usrpbeacon

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.*

/** Shared look for the app's screens (dark cards, label + control + plain-English note). */
class Ui(private val ctx: Context) {
    val bg = Color.rgb(15, 17, 21)
    val card = Color.rgb(26, 29, 36)
    val field = Color.rgb(36, 40, 49)
    val textMain = Color.rgb(237, 239, 243)
    val textSub = Color.rgb(154, 163, 178)
    val textNote = Color.rgb(112, 120, 135)
    val accent = Color.rgb(79, 140, 255)
    val green = Color.rgb(34, 170, 90)
    val amber = Color.rgb(214, 150, 30)
    val red = Color.rgb(214, 70, 70)
    val grey = Color.rgb(70, 76, 88)

    fun dp(v: Int) = (v * ctx.resources.displayMetrics.density).toInt()

    fun rounded(color: Int, radiusDp: Int) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(radiusDp).toFloat()
    }

    fun matchWrap(topDp: Int = 0) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
    ).apply { topMargin = dp(topDp) }

    fun text(t: String, sizeSp: Float, color: Int, bold: Boolean = false) = TextView(ctx).apply {
        text = t; textSize = sizeSp; setTextColor(color)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setLineSpacing(0f, 1.15f)
    }

    fun mono(t: String, sizeSp: Float = 12f, color: Int = textSub) = text(t, sizeSp, color).apply {
        typeface = Typeface.MONOSPACE
    }

    fun card(parent: LinearLayout, title: String, subtitle: String?): LinearLayout {
        val c = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(card, 16)
            setPadding(dp(18), dp(16), dp(18), dp(18))
        }
        c.addView(text(title, 17f, textMain, bold = true))
        if (subtitle != null) c.addView(text(subtitle, 13f, textSub), matchWrap(4))
        parent.addView(c, matchWrap(14))
        return c
    }

    fun setting(parent: LinearLayout, label: String, control: View, note: String) {
        parent.addView(text(label, 14f, textMain, bold = true), matchWrap(18))
        parent.addView(control, matchWrap(6))
        parent.addView(text(note, 12f, textNote), matchWrap(6))
    }

    fun input(def: String, type: Int) = EditText(ctx).apply {
        setText(def); inputType = type; textSize = 16f
        setTextColor(textMain); setHintTextColor(textNote)
        background = rounded(field, 10)
        setPadding(dp(14), dp(12), dp(14), dp(12))
        setSingleLine()
    }

    fun number(def: String, decimal: Boolean = true) = input(def,
        InputType.TYPE_CLASS_NUMBER or (if (decimal) InputType.TYPE_NUMBER_FLAG_DECIMAL else 0))

    fun spinner(items: List<String>) = Spinner(ctx).apply {
        adapter = object : ArrayAdapter<String>(ctx, android.R.layout.simple_spinner_dropdown_item, items) {
            override fun getView(pos: Int, cv: View?, p: ViewGroup): View =
                (super.getView(pos, cv, p) as TextView).apply { setTextColor(textMain); textSize = 16f }
        }
        background = rounded(field, 10)
        setPadding(dp(4), dp(4), dp(4), dp(4))
    }

    fun button(label: String, color: Int, onClick: () -> Unit) = Button(ctx).apply {
        text = label; textSize = 16f; setTextColor(Color.WHITE)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        background = rounded(color, 12)
        stateListAnimator = null
        setPadding(0, dp(14), 0, dp(14))
        setOnClickListener { onClick() }
    }

    /** Enable/disable a button made by [button] and dim it when disabled. */
    fun enable(b: Button, on: Boolean) { b.isEnabled = on; b.alpha = if (on) 1f else 0.35f }

    fun progress() = ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal).apply {
        max = 1000; progressTintList = ColorStateList.valueOf(accent)
        progressBackgroundTintList = ColorStateList.valueOf(field)
    }

    fun row(vararg views: View) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
        views.forEachIndexed { i, v ->
            addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply {
                if (i > 0) leftMargin = dp(10)
            })
        }
    }
}
