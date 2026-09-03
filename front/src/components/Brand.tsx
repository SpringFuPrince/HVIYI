import { AudioLines } from 'lucide-react'

export function Brand({ inverse = false, compact = false }: { inverse?: boolean; compact?: boolean }) {
  return (
    <div className={`brand ${inverse ? 'brand--inverse' : ''}`}>
      <span className="brand__mark"><AudioLines size={19} strokeWidth={2.4} /></span>
      {!compact && <span className="brand__name">HVIYI</span>}
    </div>
  )
}
