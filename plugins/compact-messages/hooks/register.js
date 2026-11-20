// Rendering only: no prompt, transcript, delivery, or permission hooks.
const expanded = new Set()
let handleLabels = {}

export function parsePeer(text) {
  const marker = '[ai-sessions peer message'
  if (typeof text !== 'string' || !text.startsWith(marker)) return null
  const line = text.indexOf('\n')
  const endMeta = text.indexOf('\n', line + 1)
  const start = text.indexOf('[message text]\n', endMeta)
  const end = text.lastIndexOf('\n[end peer message]')
  if (line < 0 || endMeta < 0 || start < 0 || end < start) return null
  try {
    const meta = JSON.parse(text.slice(line + 1, endMeta))
    if (!meta.message_id || !meta.sender?.tool || !meta.sender?.session_id) return null
    const handle = meta.sender_handle || handleLabels[`${meta.sender.tool}:${meta.sender.session_id}`] || `${meta.sender.tool}:${meta.sender.session_id.slice(0, 8)}`
    return { id: meta.message_id, handle, body: text.slice(start + 15, end) }
  } catch { return null }
}

function clean(text) {
  return String(text).replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\|$)/g, '')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '')
}

const graphemes = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
export function displayWidth(text) {
  let width = 0
  for (const { segment } of graphemes.segment(text)) {
    if (/\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3/u.test(segment)) { width += 2; continue }
    for (const ch of segment) {
      const n = ch.codePointAt(0)
      if (/\p{Mark}/u.test(ch) || n === 0x200d || n === 0xfe0f || n === 0xfe0e) continue
      width += n >= 0x1100 && (n <= 0x115f || n === 0x2329 || n === 0x232a ||
        (n >= 0x2e80 && n <= 0xa4cf && n !== 0x303f) || (n >= 0xac00 && n <= 0xd7a3) ||
        (n >= 0xf900 && n <= 0xfaff) || (n >= 0xfe10 && n <= 0xfe19) ||
        (n >= 0xfe30 && n <= 0xfe6f) || (n >= 0xff00 && n <= 0xff60) ||
        (n >= 0xffe0 && n <= 0xffe6) || (n >= 0x20000 && n <= 0x3fffd)) ? 2 : 1
    }
  }
  return width
}

export function preview(peer, columns = 80) {
  const line = `from ${clean(peer.handle)}: ${clean(peer.body).replace(/\s+/g, ' ').trim()}`
  const limit = Math.max(0, columns - 6)
  if (!limit) return ''
  if (displayWidth(line) <= limit) return line
  let result = ''
  for (const { segment } of graphemes.segment(line)) {
    if (displayWidth(result + segment) > limit - 1) break
    result += segment
  }
  return result + '…'
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    try {
      const labels = await $.process.run(['ses', 'messaging', 'handles', '--json'])
      handleLabels = JSON.parse(labels.stdout)
      $.ui.invalidate('ui.render')
    } catch { handleLabels = {} }
    return next(e)
  })
  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    const peer = parsePeer(e.props.text)
    if (!peer) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const isOpen = expanded.has(peer.id)
    const toggle = () => {
      if (expanded.has(peer.id)) expanded.delete(peer.id)
      else expanded.add(peer.id)
      $.ui.invalidate('ui.render')
    }
    const summary = Button({
      key: `peer-${peer.id}`, plain: true,
      label: preview(peer, e.viewport?.columns || 80), onPress: toggle,
    })
    return Box({ flexDirection: 'column', children: isOpen
      ? [summary, Text({ children: [clean(peer.body)] })] : [summary] })
  })
}

