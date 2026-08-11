# Lambda container image for the EG4 battery controller.
# Build context is the repo root so we can COPY guardrail.py + requirements.txt.
FROM public.ecr.aws/lambda/python:3.11

# Install Python deps into Lambda's task root so the runtime can import them.
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy script + thin Lambda entrypoint.
COPY guardrail.py ${LAMBDA_TASK_ROOT}/
COPY lambda/lambda_handler.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_handler.handler"]
