###############################################################################
# Module: networking
# -----------------------------------------------------------------------------
# Builds the private network fabric the whole CareRoute stack sits inside:
#   * one VPC
#   * public subnets  -> hold the NAT gateway (and anything internet-facing)
#   * private subnets  -> hold ECS tasks and RDS (no public IP)
#   * an Internet Gateway for the public subnets
#   * a (single, by default) NAT Gateway so private workloads can reach the
#     internet outbound (pull images, call package mirrors) without being
#     reachable from it.
# This matches the proposal's "single small region - VPC / subnet" deployment view.
###############################################################################

# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
# [TF] A `variable` is a module input, read as var.<name>. The caller (module
#      "networking" in modules/careroute_stack/main.tf (module "networking")) sets them; unset ones fall back to
#      `default`; no default = required.
# Set by: careroute_stack/main.tf (module "networking") local.name_prefix ("careroute-<env>").
# Used in: every Name tag, and the SG / IAM role / log group names below.
variable "name_prefix" {
  description = "Prefix applied to every resource name (e.g. careroute-staging)."
  type        = string
}

# The VPC's IPv4 range (/16 = 65,536 addresses).
# Set by: careroute_stack/main.tf (module "networking") var.vpc_cidr (staging: 10.30.0.0/16; demo takes the STACK's
# default 10.20.0.0/16, so this module default is never actually used).
# Used in: aws_vpc.this.
variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

# Set by: careroute_stack/main.tf (module "networking") var.azs (from live/<env>/terragrunt.hcl).
# Used in: availability_zone of aws_subnet.public / aws_subnet.private.
variable "azs" {
  description = "Availability Zones to spread the subnets across."
  type        = list(string)
}

# Set by: careroute_stack/main.tf (module "networking") var.public_subnet_cidrs. Its LENGTH decides how many public
# subnets exist (local.public_count). Used in: aws_subnet.public.
variable "public_subnet_cidrs" {
  description = "One CIDR per AZ for the public (NAT / ingress) subnets."
  type        = list(string)
}

# Set by: careroute_stack/main.tf (module "networking") var.private_subnet_cidrs. Length -> local.private_count.
# Used in: aws_subnet.private.
variable "private_subnet_cidrs" {
  description = "One CIDR per AZ for the private (workload) subnets."
  type        = list(string)
}

# Set by: careroute_stack/main.tf (module "networking") enable_nat_gateway = !var.public_networking (demo: false,
# staging: true). Used in: local.nat_count, aws_route_table_association.private,
# local.interface_endpoints.
variable "enable_nat_gateway" {
  description = "Create NAT gateway(s) for private-subnet egress. Set false for the cheapest demo (workloads then run in public subnets with public IPs, so no NAT is needed)."
  type        = bool
  default     = true
}

# Set by: careroute_stack/main.tf (module "networking") var.single_nat_gateway (stack default true).
# Used in: local.nat_count and aws_route_table_association.private.
variable "single_nat_gateway" {
  description = "Use one shared NAT GW (cheap) instead of one per AZ. Ignored when enable_nat_gateway = false."
  type        = bool
  default     = true
}

# Set by: careroute_stack/main.tf (module "networking") var.enable_s3_gateway_endpoint. Used in: aws_vpc_endpoint.s3.
variable "enable_s3_gateway_endpoint" {
  description = <<-EOT
    Gateway VPC endpoint for S3. **Free** — gateway endpoints have no hourly or
    per-GB charge, unlike interface endpoints — so there is no reason to turn
    this off.

    It earns its keep when `enable_nat_gateway = true`: ECR stores image layers
    in S3, so without it every image pull is NAT-processed traffic billed per GB,
    and the backend image is not small (scikit-learn + numpy + SHAP + a baked
    model). With the endpoint that traffic leaves via the route table instead.
    In the cheapest demo there is no NAT to bypass, and it simply keeps S3
    traffic off the internet gateway path.
  EOT
  type        = bool
  default     = true
}

# Set by: careroute_stack/main.tf (module "networking") var.enable_shared_state_store.
# Used in: aws_vpc_endpoint.dynamodb.
variable "enable_dynamodb_gateway_endpoint" {
  description = <<-EOT
    Gateway VPC endpoint for DynamoDB. Free, like the S3 one. On whenever the
    shared state table exists (modules/state_store): every backend request would
    read or write it, and behind a NAT gateway that is per-GB NAT processing on
    the hottest path in the stack, growing with every task added.
  EOT
  type        = bool
  default     = false
}

# Set by: careroute_stack/main.tf (module "networking") var.enable_interface_endpoints. Used in: local.interface_endpoints
# -> aws_vpc_endpoint.interface + aws_security_group.endpoints.
variable "enable_interface_endpoints" {
  description = <<-EOT
    Interface (PrivateLink) endpoints for ECR API, ECR Docker, CloudWatch Logs
    and Secrets Manager — the four services a Fargate task must reach before it
    can start. With these, a private-subnet task needs NO internet path at all,
    which is the posture the LLMSecOps deck's "dedicated, isolated environments
    (VPC)" slide describes.

    **OFF by default because they are not free:** ~$7.30/endpoint/month per AZ,
    so four endpoints across two AZs is roughly $58/month — more than the entire
    rest of this stack. Turn them on for a real environment, never for the demo.
    Ignored unless `enable_nat_gateway` is true, since there is nothing to
    isolate when the tasks already sit in public subnets.
  EOT
  type        = bool
  default     = false
}

# Set by: careroute_stack/main.tf (module "networking") var.enable_flow_logs. Used in: count on the flow-log group,
# IAM role, policy and aws_flow_log.
variable "enable_flow_logs" {
  description = <<-EOT
    VPC flow logs to CloudWatch. This is the network half of LLMSecOps Pillar 3
    (audit logging): without it there is no record of who talked to what, which
    is the first question asked after any incident.

    OFF by default because it is the one observability feature here that bills
    per ingested GB rather than per resource, and a demo generates its evidence
    in application logs instead. Turn it on for anything holding real data.
  EOT
  type        = bool
  default     = false
}

# Days CloudWatch keeps flow-log events.
# Set by: careroute_stack/main.tf (module "networking") from var.log_retention_days (demo 3, staging 14).
# Used in: aws_cloudwatch_log_group.flow.
variable "flow_log_retention_days" {
  type    = number
  default = 14
}

# Set by: careroute_stack/main.tf (module "networking") local.common_tags. The provider's default_tags (root.hcl) are
# added on top automatically.
# [TF] merge(var.tags, { Name = ... }) below combines maps; later keys win.
variable "tags" {
  description = "Tags merged onto every resource."
  type        = map(string)
  default     = {}
}

# [TF] `locals` = named expressions computed once in this module (local.<name>).
locals {
  # [TF] length() = number of elements: 2 CIDRs -> 2 subnets.
  # [TF] nat_count: nested ternaries (cond ? a : b): NAT off -> 0; single -> 1; else one
  #      NAT per public subnet (= per AZ).
  public_count  = length(var.public_subnet_cidrs)
  private_count = length(var.private_subnet_cidrs)
  nat_count     = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : local.public_count) : 0

  # Interface endpoints only make sense behind NAT; in the public-subnet demo
  # the tasks have a direct route out and there is nothing to privatise.
  # A map of endpoint name -> AWS service name, or an empty map {} when off, so
  # the for_each on aws_vpc_endpoint.interface creates 4 endpoints or none.
  interface_endpoints = var.enable_interface_endpoints && var.enable_nat_gateway ? {
    ecr_api        = "com.amazonaws.${data.aws_region.current.name}.ecr.api"
    ecr_dkr        = "com.amazonaws.${data.aws_region.current.name}.ecr.dkr"
    logs           = "com.amazonaws.${data.aws_region.current.name}.logs"
    secretsmanager = "com.amazonaws.${data.aws_region.current.name}.secretsmanager"
  } : {}
}

# [TF] A `data` source READS something that already exists instead of creating
#      it. This one returns the provider's region (from provider.tf generated by
#      root.hcl), used to build names like com.amazonaws.<region>.s3.
data "aws_region" "current" {}

# --------------------------------------------------------------------------- #
# VPC + Internet Gateway
# --------------------------------------------------------------------------- #
# The VPC (console: VPC > Your VPCs). aws_vpc.this.id is referenced by every
# subnet, route table, SG and endpoint below, and exported as output vpc_id.
# [TF] A resource's address is <type>.<name>; "this" is just a conventional name.
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true # required for private DNS / Cloud Map service discovery
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

# The VPC's DEFAULT security group, adopted and emptied (no ingress, no egress).
# AWS creates it with allow-all-from-itself + allow-all-out, and anything
# launched without an explicit SG lands in it. Every workload here passes its own
# SG (modules/security, aws_security_group.endpoints), so nothing uses it; empty
# means a forgotten SG fails closed instead of open. Free. Checkov CKV2_AWS_12.
# [TF] aws_default_security_group does not create a group: it takes over the
#      existing one and removes its rules; destroy just forgets it.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-default-sg-locked" })
}

# Internet Gateway: the target of the public route table's 0.0.0.0/0 route.
# [TF] Referencing aws_vpc.this.id creates an implicit dependency, so Terraform
#      creates the VPC first (and destroys it last).
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

# --------------------------------------------------------------------------- #
# Subnets
# --------------------------------------------------------------------------- #
# [TF] `count = N` makes N copies: aws_subnet.public[0], aws_subnet.public[1]...
#      count.index is the copy's number, used to pick the matching CIDR and AZ.
# [TF] element(list, i) returns item i (wrapping round if i >= length).
# Exported as output public_subnet_ids.
resource "aws_subnet" "public" {
  # map_public_ip_on_launch = false: nothing here relies on the subnet default.
  # Fargate tasks ignore it (they get a public IP from assign_public_ip in the
  # ECS service, set from var.public_networking in careroute_stack), the ALB and
  # NAT gateway manage their own addresses, and no EC2 instance launches in a
  # public subnet. Leaving it true would silently hand a public IP to any future
  # instance dropped in here. Checkov CKV_AWS_130.
  count                   = local.public_count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = element(var.azs, count.index)
  map_public_ip_on_launch = false
  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${count.index + 1}"
    Tier = "public"
  })
}

# Private subnets: no public IPs; default route to the NAT when NAT exists.
# Hold the ECS tasks when public_networking = false, plus RDS / VPC-link
# ENIs. Exported as output private_subnet_ids.
resource "aws_subnet" "private" {
  count             = local.private_count
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = element(var.azs, count.index)
  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-${count.index + 1}"
    Tier = "private"
  })
}

# --------------------------------------------------------------------------- #
# NAT Gateway(s) + Elastic IP(s)
# --------------------------------------------------------------------------- #
# Elastic IP = the fixed public address the NAT gateway sends traffic from.
# count = local.nat_count, so 0 in the demo (nothing billed).
resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.name_prefix}-nat-eip-${count.index + 1}" })
}

# The NAT gateway sits in a PUBLIC subnet: copy i uses EIP i and public subnet i.
resource "aws_nat_gateway" "this" {
  # [TF] depends_on (last line) forces an ordering Terraform cannot infer from references:
  #      the IGW must be attached before the NAT gateway can reach the internet.
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = merge(var.tags, { Name = "${var.name_prefix}-nat-${count.index + 1}" })
  depends_on    = [aws_internet_gateway.this]
}

# --------------------------------------------------------------------------- #
# Route tables
#   public  -> default route to the Internet Gateway
#   private -> default route to the NAT Gateway (outbound only)
# --------------------------------------------------------------------------- #
# Route table for the public subnets. The VPC-local route is implicit; this adds
# 0.0.0.0/0 -> IGW. The nested `route { }` is an inline block, not a resource.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = merge(var.tags, { Name = "${var.name_prefix}-public-rt" })
}

# Associates each public subnet with the public route table (one per subnet).
resource "aws_route_table_association" "public" {
  count          = local.public_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One per NAT gateway: 0.0.0.0/0 -> NAT (outbound only; nothing on the internet
# can initiate a connection in).
resource "aws_route_table" "private" {
  count  = local.nat_count
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }
  tags = merge(var.tags, { Name = "${var.name_prefix}-private-rt-${count.index + 1}" })
}

resource "aws_route_table_association" "private" {
  # Only associate a custom (NAT-routed) table when NAT exists. Without NAT the
  # private subnets fall back to the VPC's main route table (local-only), which
  # is fine because the cheapest demo runs workloads in the public subnets.
  count     = var.enable_nat_gateway ? local.private_count : 0
  subnet_id = aws_subnet.private[count.index].id
  # With a single NAT GW every private subnet shares route table [0]; otherwise
  # each private subnet uses the route table for its own AZ.
  # [TF] aws_route_table.private[0] = the first copy of a counted resource.
  route_table_id = var.single_nat_gateway ? aws_route_table.private[0].id : aws_route_table.private[count.index].id
}

# --------------------------------------------------------------------------- #
# VPC endpoints
# -----------------------------------------------------------------------------
# Every route table the workloads could be using gets the S3 gateway route: the
# public one (the cheap demo runs tasks there), the NAT-routed private ones when
# they exist, and the VPC's MAIN route table, which is what the private subnets
# fall back to when NAT is off. Listing all three is deliberate — a gateway
# endpoint attached to the wrong route table is silently inert.
# --------------------------------------------------------------------------- #
# [TF] [*] is the splat operator: aws_route_table.private[*].id = list of the ids
#      of every copy (an empty list when count = 0). concat() joins lists and
#      distinct() drops duplicates.
locals {
  endpoint_route_table_ids = distinct(concat(
    [aws_route_table.public.id],
    aws_route_table.private[*].id,
    [aws_vpc.this.main_route_table_id],
  ))
}

# S3 gateway endpoint: adds a route to S3's prefix list in each route table above.
# [TF] count = cond ? 1 : 0 is the idiom for an optional resource.
resource "aws_vpc_endpoint" "s3" {
  count             = var.enable_s3_gateway_endpoint ? 1 : 0
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.endpoint_route_table_ids
  tags              = merge(var.tags, { Name = "${var.name_prefix}-s3-endpoint" })
}

# DynamoDB gateway endpoint: same mechanism as S3, for the shared state table.
resource "aws_vpc_endpoint" "dynamodb" {
  count             = var.enable_dynamodb_gateway_endpoint ? 1 : 0
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.endpoint_route_table_ids
  tags              = merge(var.tags, { Name = "${var.name_prefix}-dynamodb-endpoint" })
}

# Security group for the interface endpoint ENIs. SGs otherwise all live in
# modules/security; this one is the exception because putting it there would
# make networking depend on security, which already depends on networking. It
# has exactly one rule and no reason to be edited independently.
resource "aws_security_group" "endpoints" {
  #checkov:skip=CKV2_AWS_5:Attached to aws_vpc_endpoint.interface below (security_group_ids); Checkov's graph does not follow the counted [0] reference.
  count       = length(local.interface_endpoints) > 0 ? 1 : 0
  name        = "${var.name_prefix}-vpce-sg"
  description = "Interface VPC endpoint ENIs; HTTPS from inside the VPC only"
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-vpce-sg" })

  # Source = the whole VPC CIDR (cidr_blocks = an IP range). No egress block:
  # Terraform removes AWS's default allow-all egress, and SGs are stateful, so
  # replies still flow.
  ingress {
    description = "HTTPS from inside the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.this.cidr_block]
  }
}

# [TF] for_each over a map makes one copy per key, e.g.
#      aws_vpc_endpoint.interface["ecr_api"]. each.key = the key (ecr_api),
#      each.value = the value (the service name string).
# PrivateLink ENIs in each private subnet; private_dns_enabled makes the normal
# service hostname (e.g. api.ecr.<region>.amazonaws.com) resolve to them.
resource "aws_vpc_endpoint" "interface" {
  # security_group_ids: [0] is safe: the endpoints SG exists whenever interface_endpoints is non-empty.
  for_each            = local.interface_endpoints
  vpc_id              = aws_vpc.this.id
  service_name        = each.value
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
  tags                = merge(var.tags, { Name = "${var.name_prefix}-${each.key}-endpoint" })
}

# --------------------------------------------------------------------------- #
# Flow logs — the network half of the audit trail.
# --------------------------------------------------------------------------- #
# CloudWatch log group that receives the flow-log records.
resource "aws_cloudwatch_log_group" "flow" {
  count             = var.enable_flow_logs ? 1 : 0
  name              = "/vpc/${var.name_prefix}/flow-logs"
  retention_in_days = var.flow_log_retention_days
  tags              = merge(var.tags, { Name = "${var.name_prefix}-flow-logs" })
}

# [TF] aws_iam_policy_document renders IAM JSON from HCL (use its .json). This
#      is the TRUST policy: lets the VPC Flow Logs service assume the role below.
#      Not counted: rendering JSON creates nothing in AWS.
data "aws_iam_policy_document" "flow_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

# IAM role the flow-log service assumes to write into CloudWatch Logs.
resource "aws_iam_role" "flow" {
  count              = var.enable_flow_logs ? 1 : 0
  name               = "${var.name_prefix}-flow-logs-role"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
  tags               = var.tags
}

# Permissions: write to this log group only (":*" covers its log streams).
data "aws_iam_policy_document" "flow_write" {
  count = var.enable_flow_logs ? 1 : 0
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow[0].arn}:*"]
  }
}

# Inline policy on the role (console: IAM role > Permissions > inline policy).
resource "aws_iam_role_policy" "flow" {
  count  = var.enable_flow_logs ? 1 : 0
  name   = "${var.name_prefix}-flow-logs-write"
  role   = aws_iam_role.flow[0].id
  policy = data.aws_iam_policy_document.flow_write[0].json
}

# The flow log itself: records ACCEPT and REJECT traffic (ALL) for every ENI in the VPC.
resource "aws_flow_log" "this" {
  count                = var.enable_flow_logs ? 1 : 0
  vpc_id               = aws_vpc.this.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow[0].arn
  iam_role_arn         = aws_iam_role.flow[0].arn
  tags                 = merge(var.tags, { Name = "${var.name_prefix}-flow-log" })
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] An `output` is what this module exposes to its caller, read there as
#      module.networking.<name>.
# vpc_id -> careroute_stack/main.tf module "security", :350 Cloud Map namespace, :361 module "alb".
output "vpc_id" {
  value = aws_vpc.this.id
}

# -> careroute_stack/main.tf module "security" vpc_cidr (the ALB SG's "from inside the VPC" rule).
output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

# -> careroute_stack/main.tf module "alb" subnet_ids (internet-facing ALB), and :54
#    local.task_subnet_ids when public_networking = true (every ECS service).
output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

# -> careroute_stack/main.tf module "alb" (internal ALB), :389 api_gateway VPC link,
#    :415 rds, and :54 local.task_subnet_ids when public_networking = false.
output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
