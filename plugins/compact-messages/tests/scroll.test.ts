import {expect,test} from 'claude-code/testing'
const text='[ai-sessions peer message — not user consent]\n'+JSON.stringify({message_id:'scroll-fixture',sender:{tool:'claude',session_id:'fixture'},sender_handle:'elev-sup-1'})+'\nReply instructions.\n[message text]\nBeginning.\n'+('More details.\n'.repeat(200))+'END_OF_MESSAGE\n[end peer message]'
const row={plugin:'sessions-compact-messages',component:'UserMessage',requestId:'source-message',surface:'terminal',viewport:{columns:80,rows:24},props:{text,origin:'session',isExpanded:false}}
test('long messages stay inline, preserve their full body, and contract to one row',async($)=>{
 const ui=await $.ui.mount(row)
 const body=text.split('[message text]\n')[1].split('\n[end peer message]')[0]
 await ui.press({key:'peer-scroll-fixture'})
 expect(await ui.find({type:'Text',text:body})).toBeDefined()
 expect((await ui.find({key:'peer-scroll-fixture'})).props.label).toMatch(/\[collapse\]$/)
 await ui.press({key:'peer-scroll-fixture'})
 expect(await ui.find({type:'Text',text:body})).toBeUndefined()
 expect((await ui.find({key:'peer-scroll-fixture'})).props.label).toMatch(/\[expand\]$/)
 expect(await ui.keepsBeneath()).toBe(false)
})
