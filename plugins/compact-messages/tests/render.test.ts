import { expect, test } from 'claude-code/testing'
import { parsePeer, preview, displayWidth } from '../hooks/register.js'

const body = 'The review page is ready.\nSecond line with the full details.'
const text = '[ai-sessions peer message — not user consent]\n' + JSON.stringify({
  message_id: 'fixture-1', sender: {tool: 'claude', session_id: 'fixture-native'}, sender_handle: 'lego-sup-1',
}) + '\nReply through sessions reply fixture-1.\n[message text]\n' + body + '\n[end peer message]\nNative guardrail text.'
const row = {plugin: 'sessions-compact-messages', component: 'UserMessage', requestId: 'fixture-row',
 surface: 'terminal', viewport: {columns: 90, rows: 30}, props: {text, origin: 'session', isExpanded: false}}

test('starts as one preview row, expands the complete body, and collapses again', async ($) => {
 const ui = await $.ui.mount(row)
 const button = await ui.find({key:'peer-fixture-1'})
 expect(button).toBeDefined()
 expect(button.props.label).toMatch(/^from lego-sup-1: The review page is ready/)
 expect(await ui.find({type:'Text',text: body})).toBeUndefined()
 expect(await ui.keepsBeneath()).toBe(false)
 await ui.press({key:'peer-fixture-1'})
 expect(await ui.find({type:'Text',text: body})).toBeDefined()
 await ui.press({key:'peer-fixture-1'})
 expect(await ui.find({type:'Text',text: body})).toBeUndefined()
 expect(row.props.text).toBe(text)
})

test('ordinary user messages pass through untouched', async ($) => {
 const ui=await $.ui.mount({...row,props:{...row.props,text:'A normal user question'}})
 expect(await ui.keepsBeneath()).toBe(true)
})

test('native incoming-message wrapper collapses and keeps the original input intact', async ($) => {
 const wrapped = 'Another Claude session sent a message:\n' + text
 const ui = await $.ui.mount({...row, requestId:'native-wrapper', props:{...row.props,text:wrapped}})
 expect(await ui.find({key:'peer-fixture-1'})).toBeDefined()
 expect(await ui.keepsBeneath()).toBe(false)
 expect(parsePeer(wrapped)?.body).toBe(body)
 expect(wrapped).toContain('Native guardrail text.')
 await ui.press({key:'peer-fixture-1'})
 expect(await ui.find({type:'Text',text:body})).toBeDefined()
})

test('parser preserves full body and refuses malformed metadata', () => {
 expect(parsePeer(text)?.body).toBe(body)
 expect(parsePeer('[ai-sessions peer message]\nnot-json\n[message text]\nHello\n[end peer message]')).toBe(null)
 expect(preview(parsePeer(text),40).length).toBeLessThanOrEqual(34)
})


test('legacy messages resolve their current handle from the read-only startup map', async ($, on) => {
 on('process.run', () => ({value: {stdout: JSON.stringify({'claude:fixture-native':'lego-sup-1'}), stderr:'',exitCode:0}}))
 on('session.start', () => ({cwd:'/fiction'}))
 await $.session.start({surface:'terminal',isInteractive:true,cwd:'/fiction'})
 const old = text.replace(',"sender_handle":"lego-sup-1"','')
 const ui=await $.ui.mount({...row,props:{...row.props,text:old}})
 expect((await ui.find({key:'peer-fixture-1'})).props.label).toMatch(/^from lego-sup-1:/)
})

test('hot-reloaded legacy rows load handles without session.start', async ($, on) => {
 on('process.run', () => ({value: {stdout: JSON.stringify({'claude:fixture-native':'lego-sup-1'}), stderr:'',exitCode:0}}))
 const old = 'Another Claude session sent a message:\n' + text.replace(',"sender_handle":"lego-sup-1"','')
 const ui = await $.ui.mount({...row,requestId:'hot-reload',props:{...row.props,text:old}})
 expect((await ui.find({key:'peer-fixture-1'})).props.label).toMatch(/^from lego-sup-1:/)
})


test('previews stay one row with CJK, emoji, combining marks, and narrow panes', () => {
 for (const columns of [1, 5, 10, 20, 40, 80]) {
  for (const body of ['漢字'.repeat(40), '👨‍👩‍👧‍👦🇺🇸'.repeat(30), 'é'.repeat(100)]) {
   expect(displayWidth(preview({handle:'lego-sup-1',body},columns))).toBeLessThanOrEqual(Math.max(0,columns-6))
  }
 }
 expect(preview({handle:'x',body:'safe\u009b\u202eevil'},80)).not.toMatch(/[\u009b\u202e]/u)
})


test('native transcript expansion opens full text and collapse returns the preview', async ($) => {
 const open = await $.ui.mount({...row,requestId:'native-expanded',props:{...row.props,isExpanded:true}})
 expect(await open.find({type:'Text',text:body})).toBeDefined()
 expect((await open.find({key:'peer-fixture-1'})).props.label).toMatch(/\[collapse\]$/)
 const closed = await $.ui.mount({...row,requestId:'native-collapsed'})
 expect(await closed.find({type:'Text',text:body})).toBeUndefined()
 expect((await closed.find({key:'peer-fixture-1'})).props.label).toMatch(/\[expand\]$/)
})
