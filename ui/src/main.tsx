import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '@fontsource/geist-mono/400.css'
import '@fontsource/geist-mono/500.css'
import './index.css'
import App from './App'

const container = document.getElementById('root')
if (!container) throw new Error('root element missing')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
