import '@testing-library/jest-dom'

// jsdom 不实现 scrollIntoView，ThinkingPanel/MessageBubble 等组件的自动滚动
// 在测试环境下会抛 "Not implemented"。统一替换为 no-op。
Element.prototype.scrollIntoView = () => {}
