import '@renderer/assets/main.css'
import '@renderer/assets/github-markdown-css/github-markdown.css'

// MathJax v3 classic combined component (mathjax-full, a dependency of this
// app). v3 typesets on the main thread and keeps its accessibility speech
// engine lazy/opt-in, so the strict CSP needs no worker-source grant.
import 'mathjax-full/es5/tex-svg.js'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const MJ = (window as any).MathJax
MJ.config.tex = {
  inlineMath: [['$', '$']],
  displayMath: [['$$', '$$']]
}
MJ.config.options = {
  enableMenu: false,
  enableExplorerHelp: false
}

import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'

createApp(App).use(router).mount('#app')
