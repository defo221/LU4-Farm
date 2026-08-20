/*
 * PXM v1.0 - Mouse + Keyboard HID firmware
 *
 * Target: Arduino boards with native USB HID (Leonardo / Micro / Pro Micro / Due).
 * The host sends newline-terminated text commands over serial; this sketch turns
 * them into real USB HID events. No host-side input injection is used.
 *
 * Protocol (one command per line, '\n' terminated):
 *   MOVE,<dx>,<dy>                          relative mouse move (stepped)
 *   CLICK_LEFT                              single left click (short hold)
 *   CLICK_LEFT_HOLD,<min>,<max>             left click, random hold [min..max] ms; prints actual ms
 *   CLICK_RIGHT_HOLD,<min>,<max>            right click, random hold; prints actual ms
 *   DOUBLE_CLICK,<delayMs>                  two left clicks with fixed gap (on-board)
 *   TRIPLE_CLICK,<hmin>,<hmax>,<gmin>,<gmax>  3 left clicks; prints total ms
 *   SCROLL,<steps>                          wheel scroll (+up / -down)
 *   STOP                                    abort any in-progress stepped move
 *
 *   KEY,<name>,<hold_ms>                    press + release one key
 *   KEY_COMBO,<hold_ms>,<key1>,<key2>,...   hold keys simultaneously then release all
 *
 *   --- interactive / remote-control additions ---
 *   MOUSE_DOWN,<btn>                        press and HOLD a button (left/right/middle)
 *   MOUSE_UP,<btn>                          release that button
 *   KEY_DOWN,<name>                         press and HOLD one key
 *   KEY_UP,<name>                           release that key
 *   STEP_SIZE,<n>                           set stepped-move granularity (1..127 px per
 *                                           HID report, default 10). Larger = faster long
 *                                           moves. Replies with the value applied.
 *   PING                                    replies 1; use to confirm firmware version
 *   RELEASE_ALL                             release everything held; replies 1
 *
 *   Press and release are separate commands so the host can mirror a live
 *   mouse-down ... drag ... mouse-up gesture instead of only atomic clicks.
 *   Any host driving these MUST send RELEASE_ALL when its session ends, or a
 *   button can be left held down on the board.
 *
 *   Key names (case-insensitive):
 *     enter  esc  tab  backspace  delete  insert  home  end  pageup  pagedown
 *     up  down  left  right
 *     f1 .. f12
 *     ctrl  alt  shift  (left-side modifiers)
 *     space
 *     any single printable character: a-z  0-9  symbols
 */

#include <Mouse.h>
#include <Keyboard.h>

static bool shouldStop = false;

// ---- stuck-key watchdog ----------------------------------------------------
// g_anythingHeld is set whenever a MOUSE_DOWN or KEY_DOWN command is received
// and cleared by RELEASE_ALL or by the watchdog itself.
// Two complementary triggers:
//   1. !Serial  – the host closed (or crashed on) the virtual COM port; fire
//                 immediately so keys don't stay stuck after sender exits.
//   2. timer    – if holds go silent for WATCHDOG_MS the firmware self-heals,
//                 even when the port stays open (e.g. TCP drop, Python freeze).
static bool          g_anythingHeld = false;
static unsigned long g_lastCmdMs    = 0;
static const unsigned long WATCHDOG_MS = 4000;  // 4 s without a command

static void releaseEverything() {
  Keyboard.releaseAll();
  Mouse.release(MOUSE_LEFT);
  Mouse.release(MOUSE_RIGHT);
  Mouse.release(MOUSE_MIDDLE);
  g_anythingHeld = false;
}

// Pixels emitted per HID report during a stepped move. Mouse.move() takes a
// signed char, so 127 is the hard ceiling. Small values look smoother but make
// long moves slow (each step costs one ~1 ms USB frame); remote control wants 25-50.
static int g_stepSize = 10;

void setup() {
  Serial.begin(9600);
  while (!Serial) { ; }
  delay(2000);
  Mouse.begin();
  Keyboard.begin();
  randomSeed(analogRead(A0));
}

// ---- keyboard helpers -------------------------------------------------------

static String toLower(const String& s) {
  String out = s;
  for (int i = 0; i < (int)out.length(); i++)
    out[i] = tolower(out[i]);
  return out;
}

static uint8_t nameToKeyCode(const String& raw) {
  String name = toLower(raw);
  name.trim();
  if (name == "enter"    || name == "return")   return KEY_RETURN;
  if (name == "esc"      || name == "escape")   return KEY_ESC;
  if (name == "tab")                            return KEY_TAB;
  if (name == "backspace")                      return KEY_BACKSPACE;
  if (name == "delete"   || name == "del")      return KEY_DELETE;
  if (name == "insert"   || name == "ins")      return KEY_INSERT;
  if (name == "home")                           return KEY_HOME;
  if (name == "end")                            return KEY_END;
  if (name == "pageup"   || name == "pgup")     return KEY_PAGE_UP;
  if (name == "pagedown" || name == "pgdn")     return KEY_PAGE_DOWN;
  if (name == "up")                             return KEY_UP_ARROW;
  if (name == "down")                           return KEY_DOWN_ARROW;
  if (name == "left")                           return KEY_LEFT_ARROW;
  if (name == "right")                          return KEY_RIGHT_ARROW;
  if (name == "f1")                             return KEY_F1;
  if (name == "f2")                             return KEY_F2;
  if (name == "f3")                             return KEY_F3;
  if (name == "f4")                             return KEY_F4;
  if (name == "f5")                             return KEY_F5;
  if (name == "f6")                             return KEY_F6;
  if (name == "f7")                             return KEY_F7;
  if (name == "f8")                             return KEY_F8;
  if (name == "f9")                             return KEY_F9;
  if (name == "f10")                            return KEY_F10;
  if (name == "f11")                            return KEY_F11;
  if (name == "f12")                            return KEY_F12;
  if (name == "ctrl"     || name == "lctrl")    return KEY_LEFT_CTRL;
  if (name == "rctrl")                          return KEY_RIGHT_CTRL;
  if (name == "alt"      || name == "lalt")     return KEY_LEFT_ALT;
  if (name == "ralt")                           return KEY_RIGHT_ALT;
  if (name == "shift"    || name == "lshift")   return KEY_LEFT_SHIFT;
  if (name == "rshift")                         return KEY_RIGHT_SHIFT;
  if (name == "gui" || name == "win" || name == "lwin") return KEY_LEFT_GUI;
  if (name == "rwin")                           return KEY_RIGHT_GUI;
  if (name == "capslock" || name == "caps_lock") return KEY_CAPS_LOCK;
  if (name == "space")                          return ' ';
  if (name.length() == 1)                       return (uint8_t)name[0];
  return 0;
}

// Split comma-separated tokens starting at field `startField` into keyCode array.
// Returns number of keys found.
static int parseKeys(const String& cmd, int startField, uint8_t* out, int maxKeys) {
  int count = 0;
  int idx = -1;
  // skip to startField
  for (int f = 0; f < startField; f++) {
    idx = cmd.indexOf(',', idx + 1);
    if (idx < 0) return 0;
  }
  while (count < maxKeys) {
    int next = cmd.indexOf(',', idx + 1);
    String token = (next < 0) ? cmd.substring(idx + 1) : cmd.substring(idx + 1, next);
    token.trim();
    if (token.length() == 0) break;
    uint8_t code = nameToKeyCode(token);
    if (code != 0) out[count++] = code;
    if (next < 0) break;
    idx = next;
  }
  return count;
}

// Map "left" / "right" / "middle" (or l/r/m) to a Mouse.h button constant.
// Returns 0 for an unrecognised name.
static uint8_t nameToMouseButton(const String& raw) {
  String name = toLower(raw);
  name.trim();
  if (name == "left"   || name == "l") return MOUSE_LEFT;
  if (name == "right"  || name == "r") return MOUSE_RIGHT;
  if (name == "middle" || name == "m") return MOUSE_MIDDLE;
  return 0;
}

// Return the substring after the Nth comma, trimmed (for text arguments).
static String strAfterComma(const String& s, int which) {
  int idx = -1;
  for (int i = 0; i < which; i++) {
    idx = s.indexOf(',', idx + 1);
    if (idx < 0) return String("");
  }
  int next = s.indexOf(',', idx + 1);
  String out = (next < 0) ? s.substring(idx + 1) : s.substring(idx + 1, next);
  out.trim();
  return out;
}

static void moveBySteps(int dx, int dy) {
  const int stepSize = g_stepSize;
  int dirX = (dx > 0) ? 1 : (dx < 0) ? -1 : 0;
  int dirY = (dy > 0) ? 1 : (dy < 0) ? -1 : 0;

  while ((dx != 0 || dy != 0) && !shouldStop) {
    int moveX = (dx != 0) ? dirX * min(abs(dx), stepSize) : 0;
    int moveY = (dy != 0) ? dirY * min(abs(dy), stepSize) : 0;
    Mouse.move(moveX, moveY);
    dx -= moveX;
    dy -= moveY;
    delayMicroseconds(200);
  }
}

// Stepped move used exclusively for RMB camera drags.
// Adjust DRAG_DELAY_MS to control camera rotation speed:
//   0.2 ms  (delayMicroseconds(200)) – original speed, game may drop input
//   1 ms    – 5× slower, good starting point
//   4 ms    – 20× slower, very slow
#define DRAG_STEP_PX  5
#define DRAG_DELAY_MS 1

static void moveByStepsSlow(int dx, int dy) {
  int dirX = (dx > 0) ? 1 : (dx < 0) ? -1 : 0;
  int dirY = (dy > 0) ? 1 : (dy < 0) ? -1 : 0;
  while ((dx != 0 || dy != 0) && !shouldStop) {
    int moveX = (dx != 0) ? dirX * min(abs(dx), DRAG_STEP_PX) : 0;
    int moveY = (dy != 0) ? dirY * min(abs(dy), DRAG_STEP_PX) : 0;
    Mouse.move(moveX, moveY);
    dx -= moveX;
    dy -= moveY;
    delay(DRAG_DELAY_MS);
  }
}

// returns the value after the Nth comma as int
static long argAfterComma(const String& s, int which) {
  int idx = -1;
  for (int i = 0; i < which; i++) {
    idx = s.indexOf(',', idx + 1);
    if (idx < 0) return 0;
  }
  int next = s.indexOf(',', idx + 1);
  if (next < 0) return s.substring(idx + 1).toInt();
  return s.substring(idx + 1, next).toInt();
}

void loop() {
  // ---- stuck-key watchdog ----
  // Trigger 1: host closed the virtual COM port (process killed / console closed).
  // Trigger 2: timer – something was held but no command for WATCHDOG_MS ms.
  if (g_anythingHeld) {
    if (!Serial || (millis() - g_lastCmdMs) > WATCHDOG_MS) {
      releaseEverything();
      g_lastCmdMs = millis();
    }
  }

  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;
  g_lastCmdMs = millis();  // any command resets the silence timer

  if (cmd.startsWith("MOVE")) {
    shouldStop = false;
    int dx = (int)argAfterComma(cmd, 1);
    int dy = (int)argAfterComma(cmd, 2);
    moveBySteps(dx, dy);
  }
  else if (cmd.startsWith("CLICK_LEFT_HOLD")) {
    long hmin = argAfterComma(cmd, 1);
    long hmax = argAfterComma(cmd, 2);
    if (hmax < hmin) hmax = hmin;
    long hold = random(hmin, hmax + 1);
    Mouse.press(MOUSE_LEFT);
    delay(hold);
    Mouse.release(MOUSE_LEFT);
    Serial.println(hold);
  }
  else if (cmd.startsWith("CLICK_RIGHT_HOLD")) {
    long hmin = argAfterComma(cmd, 1);
    long hmax = argAfterComma(cmd, 2);
    if (hmax < hmin) hmax = hmin;
    long hold = random(hmin, hmax + 1);
    Mouse.press(MOUSE_RIGHT);
    delay(hold);
    Mouse.release(MOUSE_RIGHT);
    Serial.println(hold);
  }
  else if (cmd.startsWith("DOUBLE_CLICK")) {
    int delayMs = (int)argAfterComma(cmd, 1);
    if (delayMs <= 0) delayMs = 100;
    Mouse.click(MOUSE_LEFT);
    delay(delayMs);
    Mouse.click(MOUSE_LEFT);
  }
  else if (cmd.startsWith("TRIPLE_CLICK")) {
    long hmin = argAfterComma(cmd, 1);
    long hmax = argAfterComma(cmd, 2);
    long gmin = argAfterComma(cmd, 3);
    long gmax = argAfterComma(cmd, 4);
    if (hmax < hmin) hmax = hmin;
    if (gmax < gmin) gmax = gmin;
    long total = 0;
    for (int i = 0; i < 3; i++) {
      long hold = random(hmin, hmax + 1);
      Mouse.press(MOUSE_LEFT);
      delay(hold);
      Mouse.release(MOUSE_LEFT);
      total += hold;
      if (i < 2) {
        long gap = random(gmin, gmax + 1);
        delay(gap);
        total += gap;
      }
    }
    Serial.println(total);
  }
  else if (cmd.startsWith("CLICK_MIDDLE")) {
    Mouse.press(MOUSE_MIDDLE);
    delay(30);
    Mouse.release(MOUSE_MIDDLE);
  }
  else if (cmd.startsWith("DRAG_RIGHT")) {
    // DRAG_RIGHT,<dx>,<dy>  — hold right button, move, release
    int dx = (int)argAfterComma(cmd, 1);
    int dy = (int)argAfterComma(cmd, 2);
    shouldStop = false;
    Mouse.press(MOUSE_RIGHT);
    delay(20);
    moveByStepsSlow(dx, dy);
    delay(20);
    Mouse.release(MOUSE_RIGHT);
  }
  else if (cmd.startsWith("CLICK_LEFT")) {
    Mouse.press(MOUSE_LEFT);
    delay(30);
    Mouse.release(MOUSE_LEFT);
  }
  else if (cmd.startsWith("SCROLL")) {
    int steps = (int)argAfterComma(cmd, 1);
    Mouse.move(0, 0, steps);
  }
  else if (cmd.equalsIgnoreCase("STOP")) {
    shouldStop = true;
    Serial.println("STOPPED");
  }
  // KEY,<name>,<hold_ms>
  else if (cmd.startsWith("KEY,")) {
    int comma2 = cmd.indexOf(',', 4);
    String keyName = (comma2 < 0) ? cmd.substring(4) : cmd.substring(4, comma2);
    long holdMs = (comma2 < 0) ? 50 : cmd.substring(comma2 + 1).toInt();
    if (holdMs <= 0) holdMs = 50;
    uint8_t code = nameToKeyCode(keyName);
    if (code != 0) {
      Keyboard.press(code);
      delay(holdMs);
      Keyboard.release(code);
    }
  }
  // KEY_COMBO,<hold_ms>,<key1>,<key2>,...
  else if (cmd.startsWith("KEY_COMBO,")) {
    long holdMs = argAfterComma(cmd, 1);
    if (holdMs <= 0) holdMs = 50;
    uint8_t keys[8];
    int n = parseKeys(cmd, 2, keys, 8);
    for (int i = 0; i < n; i++) Keyboard.press(keys[i]);
    delay(holdMs);
    Keyboard.releaseAll();
  }
  // SHIFT_CLICK_LEFT,<hold_min>,<hold_max>
  // Hold Left Shift, press LMB for a random duration, release both. Prints actual ms.
  else if (cmd.startsWith("SHIFT_CLICK_LEFT,")) {
    long hmin = argAfterComma(cmd, 1);
    long hmax = argAfterComma(cmd, 2);
    if (hmax < hmin) hmax = hmin;
    long hold = random(hmin, hmax + 1);
    Keyboard.press(KEY_LEFT_SHIFT);
    delay(10);
    Mouse.press(MOUSE_LEFT);
    delay(hold);
    Mouse.release(MOUSE_LEFT);
    delay(10);
    Keyboard.release(KEY_LEFT_SHIFT);
    Serial.println(hold);
  }
  // SHIFT_CLICK_RIGHT,<hold_min>,<hold_max>
  // Hold Left Shift, press RMB for a random duration, release both. Prints actual ms.
  else if (cmd.startsWith("SHIFT_CLICK_RIGHT,")) {
    long hmin = argAfterComma(cmd, 1);
    long hmax = argAfterComma(cmd, 2);
    if (hmax < hmin) hmax = hmin;
    long hold = random(hmin, hmax + 1);
    Keyboard.press(KEY_LEFT_SHIFT);
    delay(10);
    Mouse.press(MOUSE_RIGHT);
    delay(hold);
    Mouse.release(MOUSE_RIGHT);
    delay(10);
    Keyboard.release(KEY_LEFT_SHIFT);
    Serial.println(hold);
  }
  // MOUSE_DOWN,<btn>  — press and hold until MOUSE_UP / RELEASE_ALL
  else if (cmd.startsWith("MOUSE_DOWN,")) {
    uint8_t btn = nameToMouseButton(strAfterComma(cmd, 1));
    if (btn == 0) { Serial.println(0); return; }
    Mouse.press(btn);
    g_anythingHeld = true;
    Serial.println(1);
  }
  // MOUSE_UP,<btn>
  else if (cmd.startsWith("MOUSE_UP,")) {
    uint8_t btn = nameToMouseButton(strAfterComma(cmd, 1));
    if (btn == 0) { Serial.println(0); return; }
    Mouse.release(btn);
    Serial.println(1);
  }
  // KEY_DOWN,<name>  — press and hold until KEY_UP / RELEASE_ALL
  else if (cmd.startsWith("KEY_DOWN,")) {
    uint8_t code = nameToKeyCode(strAfterComma(cmd, 1));
    if (code == 0) { Serial.println(0); return; }
    Keyboard.press(code);
    g_anythingHeld = true;
    Serial.println(1);
  }
  // KEY_UP,<name>
  else if (cmd.startsWith("KEY_UP,")) {
    uint8_t code = nameToKeyCode(strAfterComma(cmd, 1));
    if (code == 0) { Serial.println(0); return; }
    Keyboard.release(code);
    Serial.println(1);
  }
  // STEP_SIZE,<n>  — 1..127 px per HID report during stepped moves
  else if (cmd.startsWith("STEP_SIZE,")) {
    int n = (int)argAfterComma(cmd, 1);
    if (n < 1)   n = 1;
    if (n > 127) n = 127;
    g_stepSize = n;
    Serial.println(g_stepSize);
  }
  else if (cmd.equalsIgnoreCase("PING")) {
    Serial.println(1);
  }
  else if (cmd.equalsIgnoreCase("RELEASE_ALL")) {
    releaseEverything();
    Serial.println(1);
  }
}
