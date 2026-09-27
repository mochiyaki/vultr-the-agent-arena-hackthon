
# problem statement

### **Blast Radius Zero: Safe Agent Execution on Vultr**

Build a **web-based agent that performs real work - writing and running code, or operating a real browser - with every action contained inside a sandbox running on Vultr.** Your system should act as a centralized control layer that plans a task, dispatches it to an isolated execution environment, and returns verifiable output.

Projects should demonstrate multi-step agentic workflows, real executed results rather than described ones, and a production-style web application - all running on Vultr infrastructure.

Containment-first is required. An agent that only chats is a demo; an agent that executes safely is a product.

- Deploy a **VM-based backend on Vultr (mandatory)**
- Agent LLM calls must go through **Vultr Serverless Inference (mandatory)**
- Vultr should be the **central system of control and orchestration**, not just static hosting
- Sandboxes run as containers or throwaway instances on Vultr, never inside your app process
  - Open Source Sandbox example solutions:
    - [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox)
    - [gVisor](https://gvisor.dev/)
    - [E2B Sandboxes](https://github.com/e2b-dev/e2b)
