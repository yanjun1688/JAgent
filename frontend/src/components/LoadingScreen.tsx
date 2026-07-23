import { motion } from 'motion/react'

export function LoadingScreen() {
  return (
    <div className="flex h-[60vh] items-center justify-center">
      <motion.div
        className="h-10 w-10 rounded-full border-2 border-border-soft border-t-accent-primary"
        animate={{ rotate: 360 }}
        transition={{ duration: 0.8, repeat: Infinity, ease: 'linear' }}
      />
    </div>
  )
}