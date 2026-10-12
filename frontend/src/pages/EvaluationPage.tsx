import ComingSoon from '../components/ComingSoon'
import { EvaluationIcon } from '../components/icons'

export default function EvaluationPage() {
  return (
    <ComingSoon
      icon={EvaluationIcon}
      title="Evaluation"
      description="Measure retrieval and answer quality with a reproducible evaluation set."
      planned={[
        'Curated bilingual question/answer evaluation set',
        'Retrieval metrics such as recall@k and MRR',
        'Answer faithfulness and citation accuracy checks',
        'Comparison of retrieval configurations across runs',
      ]}
    />
  )
}
