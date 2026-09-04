import '@renderer/assets/main.css'
import '@renderer/assets/github-markdown-css/github-markdown.css'

import 'mathjax/es5/tex-svg.js'
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
