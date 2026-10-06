<!-- sumbi:begin review-gates -->
# Review levels

| Work size or risk | Evidence and review |
| --- | --- |
| Small wording or local configuration | Read the resulting change and check the affected behavior. |
| Behavior across several components | Ask an independent engineering reviewer; include targeted test evidence. |
| Security, permissions, money, or irreversible operations | Require owner approval and a reviewer from a different model family; include the applicable security and release checks. |

The owner must map risky repository paths to these roles. A role is a review responsibility, not a claim that a tool enforces it. Existing stronger gates remain in force. If review is unavailable, leave the risky action pending.
<!-- sumbi:end review-gates -->
